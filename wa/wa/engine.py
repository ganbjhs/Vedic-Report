"""
Task engine: run declarative recipes from tasks.json (or Python plugins).

A task is a list of steps. Each step is {"action": "...", ...params}. Steps can
read/write named variables in a shared context; params support "{var}" and
"{var.field}" substitution. Lists can be iterated with for_each.

Built-in actions (see ACTIONS at the bottom):
  open_chat        chat
  send_text        text            [delay]
  send_file        path            [caption]
  send_lines       file [text] [delay]          -> port of the old script (line + newline + text)
  read_history     [days] [max] [since] save_as
  filter           from [senders] [exclude_me] [contains] [regex] [platform] save_as
  extract_links    from [kinds] [platforms] [dedupe] save_as   -> list of link dicts
  metrics          from [delay] save_as
  to_sheet         from [dedupe]              (uses config.sheets)
  to_csv           from path
  for_each         list | from, as, steps
  set              name, value
  print            text
  wait             seconds
  python           func ("module.function" inside ./plugins), [args]

A Python plugin gets (session, ctx, **args) and may return a value (stored in save_as).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from .chat import open_chat, send_text, send_file, send_bulk_from_file
from .reader import read_history
from .links import classify
from .metrics import fetch_metrics


# ------------------------------------------------------------ templating
_TPL = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def _lookup(ctx: dict, path: str):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def render(value, ctx: dict):
    """Recursively substitute {var} / {var.field} in strings, lists and dicts."""
    if isinstance(value, str):
        m = _TPL.fullmatch(value.strip())
        if m:  # whole string is one placeholder -> keep type (list/dict/etc.)
            v = _lookup(ctx, m.group(1))
            return v if v is not None else value
        return _TPL.sub(lambda mm: str(_lookup(ctx, mm.group(1)) if _lookup(ctx, mm.group(1)) is not None else mm.group(0)), value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value


# ------------------------------------------------------------ helpers
def _digits(s): return "".join(ch for ch in (s or "") if ch.isdigit())


def sender_matches(msg: dict, wanted: list[str]) -> bool:
    if not wanted:
        return True
    name = (msg.get("sender") or "").strip().lower()
    nums = {_digits(msg.get("phone") or ""), _digits(msg.get("sender") or "")} - {""}
    for w in wanted:
        wd = _digits(w)
        if len(wd) >= 7:
            if any(n.endswith(wd) or wd.endswith(n) for n in nums if len(n) >= 7):
                return True
        else:
            wl = w.strip().lower().lstrip("~").strip()
            if wl and (wl == name.lstrip("~").strip() or wl in name):
                return True
    return False


def links_from_messages(msgs: list[dict], group: str = "", kinds=None, platforms=None, dedupe=True) -> list[dict]:
    out, seen = [], set()
    for m in msgs:
        for url in m.get("links", []):
            c = classify(url)
            if kinds and c["kind"] not in kinds:
                continue
            if platforms and c["platform"] not in platforms:
                continue
            if dedupe and c["clean_url"] in seen:
                continue
            seen.add(c["clean_url"])
            out.append({"collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "group": group or m.get("group", ""), "sender": m.get("sender", ""),
                        "phone": m.get("phone", ""),
                        "sent_at": m["time"].strftime("%Y-%m-%d %H:%M") if m.get("time") else "",
                        "platform": c["platform"], "kind": c["kind"], "url": url,
                        "clean_url": c["clean_url"], "message": (m.get("text") or "")[:500],
                        "likes": "", "comments": "", "shares": "", "views": "", "metrics_note": ""})
    return out


# ------------------------------------------------------------ engine
class Engine:
    def __init__(self, session, cfg: dict, verbose: bool = True, log_fn=None):
        self.s = session
        self.cfg = cfg
        self.verbose = verbose
        self.log_fn = log_fn or print
        self.stop_requested = False
        self.ctx: dict = {"now": datetime.now().strftime("%Y-%m-%d %H:%M"),
                          "today": datetime.now().strftime("%Y-%m-%d"),
                          "cfg": cfg}
        self._writer = None
        self._mpage = None

    def log(self, *a):
        if self.verbose:
            self.log_fn(" ".join(str(x) for x in a))

    # -- run ---------------------------------------------------------
    def run(self, steps: list[dict], ctx: dict | None = None):
        ctx = ctx if ctx is not None else self.ctx
        for raw in steps:
            if self.stop_requested:
                raise KeyboardInterrupt("stopped by user")
            step = render(raw, ctx)
            action = step.pop("action", None)
            if action not in ACTIONS:
                raise ValueError(f"Unknown action '{action}'. Known: {sorted(ACTIONS)}")
            self.log(f"→ {action} {({k: (v if not isinstance(v, list) or len(v) < 6 else f'[{len(v)} items]') for k, v in step.items() if k != 'steps'})}")
            result = ACTIONS[action](self, ctx, **step)
            if step.get("save_as") and result is not None:
                ctx[step["save_as"]] = result
        return ctx

    # -- writers -----------------------------------------------------
    def writer(self):
        if self._writer is None:
            sc = self.cfg.get("sheets", {})
            if sc.get("mode", "gspread") == "csv":
                from .sheets import CsvWriter
                self._writer = CsvWriter(sc.get("csv_path", "links.csv"))
            else:
                from .sheets import SheetWriter
                self._writer = SheetWriter(sc["sheet_id"], sc.get("worksheet", "links"),
                                           sc.get("creds", "service_account.json"))
        return self._writer

    def metrics_page(self):
        if self._mpage is None:
            self._mpage = self.s.new_tab()
        return self._mpage


# ------------------------------------------------------------ actions
def a_open_chat(e: Engine, ctx, chat, **_):
    open_chat(e.s, chat)
    ctx["chat"] = chat


def a_send_text(e: Engine, ctx, text, delay=1.5, **_):
    send_text(e.s, str(text), delay_after=float(delay))


def a_send_file(e: Engine, ctx, path, caption="", **_):
    send_file(e.s, path, caption=str(caption))


def a_send_lines(e: Engine, ctx, file, text="", delay=1.5, chat=None, **_):
    chat = chat or ctx.get("chat")
    if not chat:
        raise ValueError("send_lines needs 'chat' or a previous open_chat")
    return send_bulk_from_file(e.s, chat, file, str(text), delay=float(delay))


def a_read_history(e: Engine, ctx, days=None, max=300, since=None, **_):
    since_dt = datetime.fromisoformat(since) if since else None
    msgs = read_history(e.s, max_messages=int(max), days=int(days) if days else None, since=since_dt)
    for m in msgs:
        m["group"] = ctx.get("chat", "")
    e.log(f"   {len(msgs)} messages")
    return msgs


def a_filter(e: Engine, ctx, **p):
    msgs = p.get("from") or []
    senders = p.get("senders") or []
    out = []
    rx = re.compile(p["regex"], re.I) if p.get("regex") else None
    for m in msgs:
        if p.get("exclude_me", True) and m.get("outgoing"):
            continue
        if not sender_matches(m, senders):
            continue
        if p.get("contains") and str(p["contains"]).lower() not in (m.get("text") or "").lower():
            continue
        if rx and not rx.search(m.get("text") or ""):
            continue
        if p.get("has_links") and not m.get("links"):
            continue
        out.append(m)
    e.log(f"   {len(out)} / {len(msgs)} kept")
    return out


def a_extract_links(e: Engine, ctx, **p):
    src = p.get("from") or []
    links = links_from_messages(src, ctx.get("chat", ""), p.get("kinds"), p.get("platforms"),
                                p.get("dedupe", True))
    e.log(f"   {len(links)} links")
    return links


def a_metrics(e: Engine, ctx, **p):
    links = p.get("from") or []
    page = e.metrics_page()
    for l in links:
        l.update(fetch_metrics(l["platform"], l["url"], page))
        e.log(f"   {l['platform']:9} likes={l['likes']} comments={l['comments']} {l['clean_url'][:60]}")
        time.sleep(float(p.get("delay", 4)))
    return links


def a_to_sheet(e: Engine, ctx, **p):
    rows = p.get("from") or []
    w = e.writer()
    if p.get("dedupe", True):
        have = w.existing_urls()
        uniq = []
        for r in rows:
            if r.get("clean_url") in have:
                continue
            have.add(r.get("clean_url"))
            uniq.append(r)
        rows = uniq
    n = w.append(rows)
    e.log(f"   wrote {n} rows")
    return n


def a_to_csv(e: Engine, ctx, path="out.csv", **p):
    import csv
    rows = p.get("from") or []
    if not rows:
        return 0
    keys = list(rows[0].keys())
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})
    return len(rows)


def a_for_each(e: Engine, ctx, steps, **p):
    items = p.get("list") if p.get("list") is not None else p.get("from")
    if isinstance(items, str):
        items = ctx.get(items, [])
    var = p.get("as", "item")
    acc = []
    for i, it in enumerate(items or []):
        sub = dict(ctx)
        sub[var] = it
        sub["index"] = i
        e.log(f"── {var} = {it if not isinstance(it, dict) else it.get('clean_url') or it.get('sender') or i}")
        e.run(steps, sub)
        # bubble up anything the loop body saved under 'collect' so callers can aggregate
        if p.get("collect") and sub.get(p["collect"]) is not None:
            v = sub[p["collect"]]
            acc.extend(v if isinstance(v, list) else [v])
    return acc if p.get("collect") else None


def a_set(e: Engine, ctx, name, value, **_):
    ctx[name] = value


def a_print(e: Engine, ctx, text="", **_):
    e.log_fn(str(text))


def a_wait(e: Engine, ctx, seconds=1, **_):
    time.sleep(float(seconds))


def a_python(e: Engine, ctx, func, args=None, **_):
    """func = 'module.function' where module is ./plugins/module.py"""
    mod_name, fn_name = func.rsplit(".", 1)
    path = Path("plugins") / f"{mod_name}.py"
    if not path.exists():
        raise FileNotFoundError(f"plugin {path} not found")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return getattr(mod, fn_name)(e.s, ctx, **(args or {}))


HELP = {
    "open_chat": "chat — open a chat/group by its exact name (sets {chat})",
    "send_text": "text, delay — send a message (\\n for new lines) into the open chat",
    "send_file": "path, caption — attach an image/video/document",
    "send_lines": "file, text, delay — each line of file → 'line⏎text' (old script)",
    "read_history": "days | since, max, save_as — scroll back and return messages",
    "filter": "from, senders, exclude_me, contains, regex, has_links, save_as — filter messages",
    "extract_links": "from, kinds, platforms, dedupe, save_as — links with platform + post/comment",
    "metrics": "from, delay, save_as — best-effort likes/comments/shares/views per link",
    "to_sheet": "from, dedupe — append rows to Google Sheet / CSV from config.sheets",
    "to_csv": "from, path — append rows to a CSV file",
    "for_each": "list | from, as, steps, collect, save_as — loop; {index} available",
    "set": "name, value — set a variable",
    "print": "text — write to the log",
    "wait": "seconds — pause",
    "python": "func ('module.fn' in plugins/), args, save_as — call your own code",
}

ACTIONS = {
    "open_chat": a_open_chat, "send_text": a_send_text, "send_file": a_send_file,
    "send_lines": a_send_lines, "read_history": a_read_history, "filter": a_filter,
    "extract_links": a_extract_links, "metrics": a_metrics, "to_sheet": a_to_sheet,
    "to_csv": a_to_csv, "for_each": a_for_each, "set": a_set, "print": a_print,
    "wait": a_wait, "python": a_python,
}

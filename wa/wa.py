#!/usr/bin/env python3
"""
wa.py — personal WhatsApp Web automation toolkit (Playwright, 100% local).

  python wa.py login                       # first run: scan QR once, profile is saved
  python wa.py send                        # feature 1: bulk send from config.json
  python wa.py send --chat "Name" --file file1.txt --text "https://x.y"
  python wa.py collect                     # feature 2/3: group links -> Google Sheet
  python wa.py collect --group "G" --sender "Rahul" --days 3 --metrics
  python wa.py read --chat "Name" -n 50    # dump recent messages (debug / explore)
  python wa.py metrics <url>               # test the metrics scraper on one link
  python wa.py debug                       # screenshot + selector health check
  python wa.py tasks                       # list recipes in tasks.json
  python wa.py run collect_links --var days=3   # run a recipe (the flexible way)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import json

from wa.paths import DATA_DIR  # noqa: F401  (sets browser env when frozen)
from wa.session import WASession, SEL
from wa.chat import open_chat, send_bulk_from_file, send_text
from wa.reader import read_history
from wa.links import classify
from wa.metrics import fetch_metrics


def load_cfg(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"config file {path} not found")
    txt = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional
        except ImportError:
            sys.exit("PyYAML not installed; use config.json instead (or `pip install pyyaml`).")
        return yaml.safe_load(txt) or {}
    return json.loads(txt) or {}


def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def sender_matches(msg: dict, wanted: list[str]) -> bool:
    """
    True if the message's sender is one of `wanted`.
    - entries with >=7 digits are treated as phone numbers: compared on digits only,
      suffix match (so '9876543210' matches '+91 98765 43210')
    - anything else is a name: case-insensitive, exact or 'contains'
    Sender data comes from the bubble header (name if saved in contacts, else the
    number) and from the message id (participant number, when WhatsApp exposes it).
    """
    if not wanted:
        return True
    name = (msg.get("sender") or "").strip().lower()
    nums = {_digits(msg.get("phone") or ""), _digits(msg.get("sender") or "")}
    nums.discard("")
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


def get_writer(cfg: dict):
    sc = cfg.get("sheets", {})
    if sc.get("mode", "gspread") == "csv":
        from wa.sheets import CsvWriter
        return CsvWriter(sc.get("csv_path", "links.csv"))
    from wa.sheets import SheetWriter
    return SheetWriter(sc["sheet_id"], sc.get("worksheet", "links"), sc.get("creds", "service_account.json"))


# --------------------------------------------------------------- commands
def cmd_login(cfg, a):
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=False) as s:
        s.wait_logged_in(timeout_s=300)
        print("Session saved. You can close this window; future runs won't need the QR.")
        input("Press Enter to exit... ")


def cmd_send(cfg, a):
    sc = cfg.get("send", {})
    delay = a.delay if a.delay is not None else sc.get("delay", 1.5)
    if a.file:
        jobs = [{"file": a.file, "text": a.text or ""}]
    else:
        jobs = sc.get("jobs", [])
    chat = a.chat or sc.get("chat")
    if not chat:
        sys.exit("No chat given (--chat or send.chat in config.json)")
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=cfg.get("headless", False)) as s:
        s.wait_logged_in()
        total = 0
        for i, job in enumerate(jobs, start=1):
            total += send_bulk_from_file(s, chat, job["file"], job.get("text", ""), delay=delay,
                                         tail=(str(i) if a.number_jobs else None))
        if a.message:
            open_chat(s, chat)
            send_text(s, a.message, delay_after=delay)
            total += 1
        print(f"Done. {total} messages sent to '{chat}'.")


def cmd_read(cfg, a):
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=cfg.get("headless", False)) as s:
        s.wait_logged_in()
        open_chat(s, a.chat)
        msgs = read_history(s, max_messages=a.n, days=a.days)
        for m in msgs:
            t = m["time"].strftime("%Y-%m-%d %H:%M") if m["time"] else "----"
            who = m['sender'] or '?'
            if m['phone'] and m['phone'] != m['sender']:
                who += f" ({m['phone']})"
            print(f"[{t}] {who}: {m['text'][:120]!r}  links={m['links']}")
        print(f"{len(msgs)} messages")
        print("\nSenders seen (copy these into collect.senders):")
        for who in sorted({(m['sender'], m['phone']) for m in msgs if m['sender']}):
            print("  ", who[0], f"  phone={who[1]}" if who[1] else "")


def cmd_collect(cfg, a):
    cc = cfg.get("collect", {})
    groups = [a.group] if a.group else cc.get("groups", [])
    senders = list(a.sender or cc.get("senders") or [])
    kinds = set(a.kind or cc.get("kinds") or ["post", "comment"])
    platforms = set(a.platform or cc.get("platforms") or [])
    days = a.days if a.days is not None else cc.get("days", 7)
    want_metrics = a.metrics or cc.get("metrics", False)
    if not groups:
        sys.exit("No groups given (--group or collect.groups in config.json)")

    writer = get_writer(cfg)
    already = writer.existing_urls()
    print(f"{len(already)} links already in sheet; new ones will be appended (duplicates skipped).")

    rows, seen = [], set()
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=cfg.get("headless", False)) as s:
        s.wait_logged_in()
        mpage = s.new_tab() if want_metrics else None
        for g in groups:
            print(f"\n=== {g} ===")
            open_chat(s, g)
            msgs = read_history(s, max_messages=cc.get("max_messages", 400), days=days)
            print(f"  scanned {len(msgs)} messages")
            for m in msgs:
                if m["outgoing"] and not cc.get("include_me", False):
                    continue
                if not sender_matches(m, senders):
                    continue
                for url in m["links"]:
                    c = classify(url)
                    if c["kind"] not in kinds:
                        continue
                    if platforms and c["platform"] not in platforms:
                        continue
                    if c["clean_url"] in seen or c["clean_url"] in already:
                        continue
                    seen.add(c["clean_url"])
                    row = {"collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "group": g, "sender": m["sender"], "phone": m["phone"],
                           "sent_at": m["time"].strftime("%Y-%m-%d %H:%M") if m["time"] else "",
                           "platform": c["platform"], "kind": c["kind"],
                           "url": url, "clean_url": c["clean_url"], "message": m["text"][:500],
                           "likes": "", "comments": "", "shares": "", "views": "", "metrics_note": ""}
                    if want_metrics:
                        row.update(fetch_metrics(c["platform"], url, mpage))
                        import time as _t; _t.sleep(cc.get("metrics_delay", 4))
                    rows.append(row)
                    print(f"  + {m['sender'][:20]:20} {c['platform']:9} {c['kind']:8} {c['clean_url'][:70]}"
                          + (f"  likes={row['likes']} comments={row['comments']}" if want_metrics else ""))
    n = writer.append(rows)
    print(f"\nWrote {n} new rows.")


def cmd_metrics(cfg, a):
    c = classify(a.url)
    print(c)
    if c["platform"] == "twitter":
        print(fetch_metrics("twitter", a.url))
        return
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=False) as s:
        page = s.new_tab()
        print(fetch_metrics(c["platform"], a.url, page))


def cmd_debug(cfg, a):
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=False) as s:
        s.wait_logged_in()
        if a.chat:
            open_chat(s, a.chat)
        for key, alts in SEL.items():
            hits = [(css, s.page.locator(css).count()) for css in alts]
            ok = [f"{css} ×{n}" for css, n in hits if n]
            print(f"{key:12} {'OK  ' if ok else 'MISS'} {ok or alts}")
        print("\nEditable elements on the page (paste this if a selector is MISSing):")
        for e in s.dump_editors():
            print("  ", e)
        s.screenshot("debug.png")


def cmd_tasks(cfg, a):
    tasks = json.loads(Path(a.tasks).read_text(encoding="utf-8"))
    for name, t in tasks.items():
        if name.startswith("_"):
            continue
        print(f"{name:24} {t.get('description', '')}")
        if t.get("vars"):
            print(f"{'':24} vars: {t['vars']}")


def cmd_run(cfg, a):
    from wa.engine import Engine
    tasks = json.loads(Path(a.tasks).read_text(encoding="utf-8"))
    if a.task not in tasks:
        sys.exit(f"task '{a.task}' not in {a.tasks}. Available: {[k for k in tasks if not k.startswith('_')]}")
    t = tasks[a.task]
    overrides = {}
    for kv in a.var or []:
        k, _, v = kv.partition("=")
        try:
            v = json.loads(v)  # allows --var days=3  --var 'groups=["A","B"]'
        except json.JSONDecodeError:
            pass
        overrides[k] = v
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=cfg.get("headless", False)) as s:
        s.wait_logged_in()
        eng = Engine(s, cfg)
        eng.ctx.update(t.get("vars", {}))
        eng.ctx.update(overrides)
        eng.run(t["steps"])
        print("Task finished.")


# --------------------------------------------------------------- argparse
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="config.json", help="config.json (or .yaml if PyYAML installed)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(fn=cmd_login)

    q = sub.add_parser("send")
    q.add_argument("--chat"); q.add_argument("--file"); q.add_argument("--text")
    q.add_argument("--message", help="send a single message instead of / in addition to files")
    q.add_argument("--delay", type=float)
    q.add_argument("--number-jobs", action="store_true", help="send job index after each file (like the old script)")
    q.set_defaults(fn=cmd_send)

    q = sub.add_parser("read")
    q.add_argument("--chat", required=True); q.add_argument("-n", type=int, default=50)
    q.add_argument("--days", type=int)
    q.set_defaults(fn=cmd_read)

    q = sub.add_parser("collect")
    q.add_argument("--group"); q.add_argument("--sender", action="append")
    q.add_argument("--kind", action="append"); q.add_argument("--platform", action="append")
    q.add_argument("--days", type=int); q.add_argument("--metrics", action="store_true")
    q.set_defaults(fn=cmd_collect)

    q = sub.add_parser("metrics"); q.add_argument("url"); q.set_defaults(fn=cmd_metrics)

    q = sub.add_parser("tasks", help="list tasks in tasks.json")
    q.add_argument("--tasks", default="tasks.json"); q.set_defaults(fn=cmd_tasks)

    q = sub.add_parser("run", help="run a task from tasks.json")
    q.add_argument("task"); q.add_argument("--tasks", default="tasks.json")
    q.add_argument("--var", action="append", help="override a task var, e.g. --var days=3 --var 'groups=[\"A\"]'")
    q.set_defaults(fn=cmd_run)

    q = sub.add_parser("debug"); q.add_argument("--chat"); q.set_defaults(fn=cmd_debug)

    a = p.parse_args()
    a.fn(load_cfg(a.config), a)


if __name__ == "__main__":
    main()

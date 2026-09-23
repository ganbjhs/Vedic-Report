#!/usr/bin/env python3
"""
bot.py — a WhatsApp *command bot* that you drive from inside a WhatsApp group.

    python bot.py                 # runs quietly (headless) until Ctrl+C
    python bot.py --headed        # show the browser window (first login / debugging)

Setup once:  python wa.py login   (scan QR)  →  set  bot.group  in config.json
             (the group where you will type commands; the bot = your own account)

Talk to it in that group, from your phone:

    /start        → bot asks: reply 1 = messages only, 2 = messages + link
    1  or  2
    <message list>            one WhatsApp message = one list;
                              inside it, messages are separated by a BLANK LINE
    <link>                    (mode 2 only) send the link for the list you just sent
    ... more lists (and links) ...
    /run          → bot sends every message of list 1 one by one, then "1",
                    then list 2 ..., then "2", and so on. In mode 2 every
                    message gets its link appended on a new line.
    /cancel       → forget everything, back to idle
    /status       → what the bot has collected so far
    /target Name  → send the output into another chat instead of this group
    /help

All bot replies start with 🔹 so they are easy to tell apart from your lists. Nothing else is
printed anywhere — the group *is* the log.

Who may drive it: a job belongs to whoever sent /start. Until that job ends
(/run, /cancel, or 30 min of silence) other people's messages in the group are
ignored — so adding someone to the group can no longer turn their "hi" into a
list. /status and /help work for everyone. (config.json → bot.lock_owner)

Every new message is handled, in order. A command that arrives while the bot
is still typing its previous reply is not dropped; a reply that fails is
retried; after repeated failures the page is reloaded and the group re-opened.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wa.paths import DATA_DIR, CONFIG as DEFAULT_CONFIG   # noqa: E402
BASE = DATA_DIR
os.chdir(BASE)

from wa.session import WASession                     # noqa: E402
from wa.chat import open_chat, send_text              # noqa: E402
from wa.reader import read_visible                    # noqa: E402

BOT = "🔹 "   # visible marker on every bot reply (easy to tell apart from your content; change if you like)
URL_RE = re.compile(r"https?://\S+", re.I)


_ZW = "\u200b\u200c\u200d\u2060\ufeff"          # zero-width characters phones sneak into "blank" lines


def split_list(text: str) -> list[str]:
    """One WhatsApp message → individual messages, separated by blank lines.

    A "blank" line may hold spaces, tabs, non-breaking spaces or zero-width
    characters (copy-pasted lists do this) — it still separates messages."""
    parts = re.split(r"\n(?:[^\S\n]|[" + _ZW + r"])*\n+", text.strip())
    return [p.strip() for p in parts if p.strip(" \t\r\n" + _ZW)]


def is_link_only(text: str) -> bool:
    t = text.strip()
    return bool(URL_RE.fullmatch(t)) or (len(t.split()) == 1 and t.lower().startswith(("http://", "https://", "www.")))


class Bot:
    def __init__(self, s: WASession, group: str, delay: float = 1.5, poll: float = 2.0, target: str | None = None,
                 ignore_own: bool = True, lock_owner: bool = True, job_ttl: float = 1800.0,
                 max_errors: int = 4, burst_limit: int = 25):
        self.s = s
        self.ignore_own = ignore_own   # True when the bot runs on its own number: never react to what it sends
        self.lock_owner = lock_owner   # a job belongs to whoever sent /start; others are ignored until it ends
        self.job_ttl = job_ttl         # seconds of silence after which an open job is dropped
        self.max_errors = max_errors   # consecutive failed polls before the page is reloaded
        self.burst_limit = burst_limit # more "new" messages than this in one poll = a re-render, not commands
        self.group = group
        self.delay = delay
        self.poll = poll
        self.target = target or group
        self.state = "idle"          # idle | mode | collect
        self.mode = None             # 1 | 2
        self.lists: list[dict] = []  # [{"messages": [...], "link": str|None}]
        self.owner: str | None = None   # sender who ran /start (display name or number as WhatsApp shows it)
        self.last_cmd_at = time.time()
        self.seen: dict[str, float] = {}   # message key -> when first seen (bounded, see _trim_seen)
        self.retry: dict[str, int] = {}    # message key -> failed attempts
        self.waited: dict[str, int] = {}   # message key -> polls spent waiting for "Read more" to finish
        self.max_wait_polls = 3            # then the message is handled as it is, with a warning
        self.errors = 0                    # consecutive failed polls
        self.polls = 0

    # ---------------------------------------------------------------- io
    def say(self, text: str):
        """Send a bot reply. One retry after clearing overlays: a tooltip over the
        composer used to cost a 15 s timeout AND the command that triggered it."""
        print(f"[bot] -> {text[:70]!r}", flush=True)
        try:
            send_text(self.s, BOT + text, delay_after=0.6)
        except Exception as e:  # noqa: BLE001
            print(f"[bot] send failed ({type(e).__name__}: {str(e).splitlines()[0][:80]}) — retrying once", flush=True)
            try:
                self.s.dismiss_dialogs()
                self.s.clear_overlays()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
            send_text(self.s, BOT + text, delay_after=0.6)

    @staticmethod
    def _norm(name: str | None) -> str:
        """Letters+digits only, lower-case. The header's innerText drops emoji
        (they are <img alt>), so 'Bot 🤖' must still match 'Bot'."""
        return re.sub(r"[^0-9a-z]+", "", (name or "").lower())

    def in_group(self):
        shown = self.s.current_chat().strip()
        a, b = self._norm(shown), self._norm(self.group)
        if a and b and (a == b or a in b or b in a):
            return
        if not shown and self.seen:
            # header not readable right now but we were in the group before -> don't thrash
            return
        print(f"[bot] header shows {shown!r}; (re)opening '{self.group}'", flush=True)
        last = None
        for attempt in range(3):
            try:
                self.s.dismiss_dialogs()
                open_chat(self.s, self.group)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2)
        raise RuntimeError(f"Could not open the group '{self.group}'. Check the exact name (as shown in "
                           f"WhatsApp) and make sure it is in your recent chats. Details: {last}")

    # ---------------------------------------------------------------- message identity
    @staticmethod
    def keys_for(msgs: list[dict]) -> list[str]:
        """One stable key per visible message.

        WhatsApp's message id when the DOM exposes one. Otherwise time + sender +
        text, plus an occurrence index, so two identical commands in the same
        minute ("/run", "/run") are two messages, not one already-seen one."""
        counts: dict[str, int] = {}
        out = []
        for m in msgs:
            if m.get("id"):
                out.append("id:" + m["id"])
                continue
            base = f"noid:{m.get('time')}:{m.get('sender')}:{(m.get('text') or '')[:60]}"
            n = counts.get(base, 0)
            counts[base] = n + 1
            out.append(f"{base}#{n}")
        return out

    def _mark_seen(self, keys) -> None:
        now = time.time()
        for k in keys:
            self.seen.setdefault(k, now)
        self._trim_seen()

    def _trim_seen(self, keep: int = 3000) -> None:
        if len(self.seen) > keep:
            for k in sorted(self.seen, key=self.seen.get)[: len(self.seen) - keep]:
                self.seen.pop(k, None)
                self.retry.pop(k, None)
                self.waited.pop(k, None)

    def snapshot_seen(self):
        self._mark_seen(self.keys_for(read_visible(self.s, expand=False)))

    # ---------------------------------------------------------------- loop
    def run_forever(self):
        self.in_group()
        self.snapshot_seen()          # ignore history
        self.say("Online. Send /start to begin, /help for commands.")
        self.snapshot_seen()
        print(f"Bot listening in '{self.group}' (Ctrl+C to stop)", flush=True)
        while True:
            try:
                self.poll_once()
                self.errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                print(f"[bot] error ({self.errors}/{self.max_errors}): {type(e).__name__}: {e}", flush=True)
                if self.errors >= self.max_errors:
                    self.recover()
                time.sleep(min(3 * self.errors, 15))
            time.sleep(self.poll)

    def poll_once(self):
        self.in_group()
        msgs = read_visible(self.s)
        keys = self.keys_for(msgs)
        self.polls += 1
        if self.polls % 30 == 0:
            print(f"[bot] alive, {len(msgs)} msgs visible, state={self.state}, owner={self.owner!r}", flush=True)
        self.expire_job()
        new = [(k, m) for k, m in zip(keys, msgs) if k not in self.seen]
        if not new:
            return
        if len(new) > self.burst_limit:
            # A reload or re-render shows the whole window as "new". Commands
            # from before are history; only the tail could be live, and there
            # is no way to tell — so log it and treat everything as seen.
            print(f"[bot] {len(new)} unseen messages at once — treating as history, not commands", flush=True)
            self._mark_seen(k for k, _ in new)
            return
        for i, (k, m) in enumerate(new):
            self.seen[k] = time.time()
            text = (m.get("text") or "").strip()
            why = self.skip_reason(m, text)
            if why:
                if text:
                    print(f"[bot] ignored ({why}): {text[:50]!r} from {m.get('sender')!r}", flush=True)
                continue
            if m.get("truncated") and self.waited.get(k, 0) < self.max_wait_polls:
                # WhatsApp still shows "Read more" on this bubble: the text is
                # incomplete and counting it now would under-count the list.
                # Leave it (and everything after it) unseen and try next poll.
                self.waited[k] = self.waited.get(k, 0) + 1
                for kk, _ in new[i:]:
                    self.seen.pop(kk, None)
                print(f"[bot] message still truncated (Read more) — waiting ({self.waited[k]}/{self.max_wait_polls})", flush=True)
                break
            print(f"[bot] <- {text[:70]!r} from {m.get('sender')!r}", flush=True)
            try:
                self.handle(text, m)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                # Do not lose the command: forget this and every later message in
                # the batch so the next poll retries them in order (twice, max).
                n = self.retry.get(k, 0) + 1
                self.retry[k] = n
                if n <= 2:
                    for kk, _ in new[i:]:
                        self.seen.pop(kk, None)
                    print(f"[bot] handling failed ({type(e).__name__}); retry {n}/2 next poll", flush=True)
                else:
                    print(f"[bot] giving up on {text[:50]!r} after {n - 1} retries", flush=True)
                raise
        self._trim_seen()

    def skip_reason(self, m: dict, text: str) -> str | None:
        if not text:
            return "no text"                      # media-only or system row
        if self.ignore_own and m.get("outgoing"):
            return "own message"
        if text.startswith(BOT.strip()):
            return "bot reply"
        if self.lock_owner and self.owner and self.state != "idle":
            sender = m.get("sender") or ""
            low = text.lower()
            if sender and self._norm(sender) != self._norm(self.owner) \
                    and not low.startswith(("/status", "/help")):
                return f"job belongs to {self.owner!r}"
        return None

    def expire_job(self):
        if self.state != "idle" and self.job_ttl and time.time() - self.last_cmd_at > self.job_ttl:
            who = self.owner or "someone"
            self.reset()
            self.say(f"Job by {who} expired after {int(self.job_ttl // 60)} min of silence. Send /start to begin again.")

    def recover(self):
        """Self-heal after repeated failed polls: reload WhatsApp Web and re-open the group."""
        print("[bot] too many errors in a row — reloading WhatsApp Web", flush=True)
        self.errors = 0
        try:
            self.s.page.reload(wait_until="domcontentloaded")
            self.s.wait_logged_in(timeout_s=120)
            self.s.dismiss_dialogs()
            open_chat(self.s, self.group)
            self.snapshot_seen()
            print("[bot] recovered", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[bot] recovery failed: {type(e).__name__}: {e}", flush=True)

    # ---------------------------------------------------------------- commands
    def handle(self, text: str, m: dict | None = None):
        low = text.lower()
        self.last_cmd_at = time.time()
        if low.startswith("/help"):
            return self.say("/start – new job\n1 or 2 – messages only / messages + link\n"
                            "then send lists (blank line between messages; in mode 2 send the link after each list)\n"
                            "/run – send everything\n/status · /cancel · /target <chat>")
        if low.startswith("/cancel"):
            self.reset()
            return self.say("Cleared. Send /start to begin again.")
        if low.startswith("/status"):
            return self.say(self.status_text())
        if low.startswith("/target"):
            name = text[7:].strip()
            if not name:
                return self.say(f"Output goes to '{self.target}'. Use /target <chat name> to change.")
            self.target = name
            return self.say(f"Output will go to '{name}'.")
        if low.startswith("/start"):
            self.reset()
            self.state = "mode"
            self.owner = (m or {}).get("sender") or None
            return self.say("New job. Reply 1 for messages only, 2 for messages + link.")
        if low.startswith("/run"):
            if self.state != "collect" or not self.lists:
                return self.say("Nothing to run yet. Send /start first.")
            if self.mode == 2 and any(l["link"] is None for l in self.lists):
                return self.say(f"List {len(self.lists)} still needs its link. Send it, then /run.")
            return self.execute()

        if self.state == "mode":
            if low in ("1", "2"):
                self.mode = int(low)
                self.state = "collect"
                hint = ("Send your message list (blank line between messages)." +
                        (" After each list, send its link." if self.mode == 2 else "") +
                        " Then /run.")
                return self.say(hint)
            return self.say("Reply 1 (messages only) or 2 (messages + link).")

        if self.state == "collect":
            if self.mode == 2 and is_link_only(text):
                if not self.lists or self.lists[-1]["link"] is not None:
                    return self.say("Send the message list first, then its link.")
                self.lists[-1]["link"] = text.strip()
                return self.say(f"Link added to list {len(self.lists)}. Next list, or /run.")
            items = split_list(text)
            self.lists.append({"messages": items, "link": None})
            need = ". Now send its link" if self.mode == 2 else ""
            last = items[-1].splitlines()[0][:28] if items else ""
            tail = f" (last: \u201c{last}\u2026\u201d)" if last else ""
            warn = (" \u26a0 WhatsApp cut this message off (Read more) \u2014 if the count is short, /cancel and send it as smaller lists."
                    if (m or {}).get("truncated") else "")
            return self.say(f"List {len(self.lists)}: {len(items)} message{'s' if len(items) != 1 else ''}{tail}{need}. Next list, or /run.{warn}")
        # idle: ignore normal chatter

    def status_text(self) -> str:
        if self.state == "idle":
            return "Idle. Send /start to begin."
        who = f" (job by {self.owner})" if self.owner else ""
        if self.state == "mode":
            return f"Waiting for 1 or 2{who}."
        mode = "messages + link" if self.mode == 2 else "messages only"
        parts = [f"Mode: {mode}{who}. {len(self.lists)} list{'s' if len(self.lists) != 1 else ''} ready, output → '{self.target}'."]
        for i, l in enumerate(self.lists, 1):
            n = len(l['messages'])
            extra = "" if self.mode != 2 else (" · link ✓" if l["link"] else " · link missing")
            parts.append(f"  {i}. {n} message{'s' if n != 1 else ''}{extra}")
        parts.append("Send /run to go.")
        return "\n".join(parts)

    def reset(self):
        self.state, self.mode, self.lists, self.owner = "idle", None, [], None

    # ---------------------------------------------------------------- execution
    def execute(self):
        total = sum(len(l["messages"]) for l in self.lists)
        self.say(f"Sending {total} message{'s' if total != 1 else ''} in {len(self.lists)} list{'s' if len(self.lists) != 1 else ''}…")
        if self._norm(self.target) != self._norm(self.group):
            open_chat(self.s, self.target)
        try:
            for i, l in enumerate(self.lists, 1):
                for msg in l["messages"]:
                    full = f"{msg}\n{l['link']}" if (self.mode == 2 and l["link"]) else msg
                    send_text(self.s, full, delay_after=self.delay)
                send_text(self.s, str(i), delay_after=self.delay)   # numbering after each list
        finally:
            if self._norm(self.target) != self._norm(self.group):
                open_chat(self.s, self.group)
        nlists = len(self.lists)
        self.reset()
        self.snapshot_seen()   # don't re-read what we just sent
        self.say(f"Done. {total} message{'s' if total != 1 else ''} sent in {nlists} list{'s' if nlists != 1 else ''}. Send /start for the next job.")
        self.snapshot_seen()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", help="control group name (default: config.json → bot.group)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--delay", type=float)
    ap.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    bc = cfg.get("bot", {})
    group = a.group or bc.get("group")
    if not group:
        sys.exit("Set the control group: python bot.py --group \"My Bot Group\"  (or bot.group in config.json)")
    headless = (not a.headed) and bc.get("headless", True)
    delay = a.delay if a.delay is not None else bc.get("delay", 1.5)

    print(f"Starting {'headless' if headless else 'headed'} — group '{group}'")
    with WASession(cfg.get("profile_dir", "./wa_profile"), headless=headless) as s:
        try:
            s.wait_logged_in(timeout_s=90 if headless else 300)
        except TimeoutError:
            if headless:
                sys.exit("Could not see WhatsApp logged in while headless. Run once with --headed "
                         "(scan QR if asked), then try headless again.")
            raise
        bot = Bot(s, group, delay=delay, poll=bc.get("poll", 2.0), target=bc.get("target"),
                  ignore_own=bc.get("ignore_own", True), lock_owner=bc.get("lock_owner", True),
                  job_ttl=float(bc.get("job_ttl_seconds", 1800)))
        try:
            bot.run_forever()
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()

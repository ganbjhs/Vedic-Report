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


def split_list(text: str) -> list[str]:
    """One WhatsApp message → individual messages, separated by blank lines."""
    parts = re.split(r"\n[ \t]*\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def is_link_only(text: str) -> bool:
    t = text.strip()
    return bool(URL_RE.fullmatch(t)) or (len(t.split()) == 1 and t.lower().startswith(("http://", "https://", "www.")))


class Bot:
    def __init__(self, s: WASession, group: str, delay: float = 1.5, poll: float = 2.0, target: str | None = None,
                 ignore_own: bool = True):
        self.s = s
        self.ignore_own = ignore_own   # True when the bot runs on its own number: never react to what it sends
        self.group = group
        self.delay = delay
        self.poll = poll
        self.target = target or group
        self.state = "idle"          # idle | mode | collect
        self.mode = None             # 1 | 2
        self.lists: list[dict] = []  # [{"messages": [...], "link": str|None}]
        self.seen: set[str] = set()

    # ---------------------------------------------------------------- io
    def say(self, text: str):
        print(f"[bot] -> {text[:70]!r}", flush=True)
        send_text(self.s, BOT + text, delay_after=0.6)

    def in_group(self):
        shown = self.s.current_chat().strip().lower()
        if shown == self.group.strip().lower():
            return
        if shown and self.group.strip().lower() in shown:
            return
        if not shown and self.seen:
            # header not readable right now but we were in the group before -> don't thrash
            return
        print(f"[bot] header shows {shown!r}; (re)opening '{self.group}'", flush=True)
        last = None
        for attempt in range(3):
            try:
                open_chat(self.s, self.group)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2)
        raise RuntimeError(f"Could not open the group '{self.group}'. Check the exact name (as shown in "
                           f"WhatsApp) and make sure it is in your recent chats. Details: {last}")

    def snapshot_seen(self):
        for m in read_visible(self.s, expand=False):
            self.seen.add(m["id"] or f"noid:{m['time']}:{(m['text'] or '')[:40]}")

    # ---------------------------------------------------------------- loop
    def run_forever(self):
        self.in_group()
        self.snapshot_seen()          # ignore history
        self.say("Online. Send /start to begin, /help for commands.")
        self.snapshot_seen()
        print(f"Bot listening in '{self.group}' (Ctrl+C to stop)", flush=True)
        n = 0
        while True:
            try:
                self.in_group()
                msgs = read_visible(self.s)
                n += 1
                if n % 30 == 0:
                    print(f"[bot] alive, {len(msgs)} msgs visible, state={self.state}", flush=True)
                # Only the LAST message in the chat counts as a command; everything
                # older is remembered as seen so it is never replayed.
                new = []
                for m in msgs:
                    mid = m["id"] or f"noid:{m['time']}:{(m['text'] or '')[:40]}"
                    if mid in self.seen:
                        continue
                    self.seen.add(mid)
                    new.append(m)
                if new:
                    m = new[-1]
                    text = (m["text"] or "").strip()
                    if self.ignore_own and m["outgoing"]:
                        text = ""            # our own message (or the account owner's) — never a command
                    if text and not text.startswith(BOT.strip()):
                        print(f"[bot] <- {text[:70]!r} from {m['sender']!r}", flush=True)
                        self.handle(text)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[bot] error: {type(e).__name__}: {e}", flush=True)
                time.sleep(3)
            time.sleep(self.poll)

    # ---------------------------------------------------------------- commands
    def handle(self, text: str):
        low = text.lower()
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
            return self.say(f"List {len(self.lists)}: {len(items)} message{'s' if len(items) != 1 else ''}{need}. Next list, or /run.")
        # idle: ignore normal chatter

    def status_text(self) -> str:
        if self.state == "idle":
            return "Idle. Send /start to begin."
        if self.state == "mode":
            return "Waiting for 1 or 2."
        mode = "messages + link" if self.mode == 2 else "messages only"
        parts = [f"Mode: {mode}. {len(self.lists)} list{'s' if len(self.lists) != 1 else ''} ready, output → '{self.target}'."]
        for i, l in enumerate(self.lists, 1):
            n = len(l['messages'])
            extra = "" if self.mode != 2 else (" · link ✓" if l["link"] else " · link missing")
            parts.append(f"  {i}. {n} message{'s' if n != 1 else ''}{extra}")
        parts.append("Send /run to go.")
        return "\n".join(parts)

    def reset(self):
        self.state, self.mode, self.lists = "idle", None, []

    # ---------------------------------------------------------------- execution
    def execute(self):
        total = sum(len(l["messages"]) for l in self.lists)
        self.say(f"Sending {total} message{'s' if total != 1 else ''} in {len(self.lists)} list{'s' if len(self.lists) != 1 else ''}…")
        if self.target.lower() != self.group.lower():
            open_chat(self.s, self.target)
        try:
            for i, l in enumerate(self.lists, 1):
                for msg in l["messages"]:
                    full = f"{msg}\n{l['link']}" if (self.mode == 2 and l["link"]) else msg
                    send_text(self.s, full, delay_after=self.delay)
                send_text(self.s, str(i), delay_after=self.delay)   # numbering after each list
        finally:
            if self.target.lower() != self.group.lower():
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
                  ignore_own=bc.get("ignore_own", True))
        try:
            bot.run_forever()
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()

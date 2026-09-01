#!/usr/bin/env python3
"""Message splitter — Bot 2 (docs/telegram-plan.md §4).

Takes a batch of message LISTS and sends every message of list 1 into a chat
one at a time, then `1`, then all of list 2, then `2`, and so on. The numbering
is the point: it marks where one list ends in the destination chat, which is
what the WhatsApp tool this replaces existed to do.

    tag it in a group     ->  "Continue in your DM"  (the output is many
                              messages; composing it is chatty too, and a
                              group should not have to watch that happen)
    /start in a DM        ->  the compose panel

    Split       [Blank line] [Per line]        both rules, chosen per batch
    Under each  [Nothing] [A link per list] [One fixed text]
    Send to     [My DM] [This group] [Another chat...]

    then one message per list  ->  Send  ->  paced delivery with a progress
    bar, a receipt if it went somewhere else, and Undo while the window lasts.

Two splitting rules because the WhatsApp toolkit had two and both were used:
blank-line (the convention colleagues know) and per-line (the `send_lines`
recipe, one fixed piece of text under every line). Offering both costs one tap
and picking one for people would break half of what they do.

Run:  python splitter.py
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

try:                                      # `python -m tg.splitter`
    from .client import ApiError, TokenClient
except ImportError:                       # `python splitter.py`
    from client import ApiError, TokenClient

log = logging.getLogger("splitter")
HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _load_env():
    for name in (".env.splitter", ".env"):
        path = HERE / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split("#")[0].strip().strip('"').strip("'"))


_load_env()

BOT_TOKEN = os.environ.get("TG_SPLITTER_TOKEN", "").strip()
ALLOWED = {int(x) for x in os.environ.get("TG_ALLOWED_IDS", "").replace(" ", "").split(",")
           if x.strip().isdigit()}
# Fallback gate for "send into a chat you are not standing in", used only when
# no Report Maker token is configured. With one, the scope decides.
SEND_OTHER = {int(x) for x in os.environ.get("TG_SEND_OTHER_IDS", "").replace(" ", "").split(",")
              if x.strip().isdigit()}

APP_URL = os.environ.get("APP_URL", "").strip()
RM_TOKEN = os.environ.get("RM_TOKEN", "").strip()

# Telegram allows roughly 20 messages a minute into one group, about 30 a
# second overall. A 60-message batch therefore takes ~3 minutes no matter what,
# so the bot paces itself rather than firing a burst: a throttle that split a
# list in half would be far worse than slowness.
GROUP_INTERVAL = float(os.environ.get("TG_GROUP_INTERVAL", "3.1") or 3.1)
DM_INTERVAL = float(os.environ.get("TG_DM_INTERVAL", "1.0") or 1.0)

# Telegram lets a bot delete its own messages for 48 hours. The Undo button is
# offered for a much shorter window, because "undo" that appears next to work
# from yesterday is a trap, not a feature.
UNDO_MINUTES = float(os.environ.get("TG_UNDO_MINUTES", "30") or 30)

MAX_MESSAGES = int(os.environ.get("TG_MAX_MESSAGES", "400") or 400)
MAX_LEN = 4096                              # Telegram's per-message ceiling

DATA_DIR = Path(os.environ.get("TG_DATA_DIR", str(HERE / "data")))
BOT_USERNAME = ""

# --------------------------------------------------------------------------- #
# Features
#
# A feature is a piece of the bot that can be switched off without being taken
# out. That distinction matters: code deleted to "simplify" is code that has to
# be rewritten from memory when somebody wants it back, and a flag costs one
# dict lookup.
#
# Resolved in layers, most specific last:
#     the default below  ->  TG_FEATURE_<NAME> in the env  ->  the bot registry
#
# The registry layer only applies when this bot has a Report Maker token; with
# no token the first two layers still work, so the splitter never stops being
# usable on its own. Same shape as the `send.other_chat` gate.
# --------------------------------------------------------------------------- #
FEATURES = {
    "preview": {
        "label": "Preview before sending",
        "default": False,
        "why": "A dry-run listing of what will land. Off: Undo already answers "
               "'that was the wrong batch', and it answers it after the fact, "
               "which is when people actually notice.",
    },
    "text_commands": {
        "label": "Type 'done' instead of tapping",
        "default": True,
        "why": "A message that is only a number closes a list; one that is "
               "only 'done' finishes the batch. Both also have buttons.",
    },
    "undo": {
        "label": "Undo after sending",
        "default": True,
        "why": "Deletes everything the batch just sent, for a while afterwards.",
    },
    "receipt": {
        "label": "Receipt in your DM",
        "default": True,
        "why": "A line back in your own chat when the delivery went elsewhere.",
    },
    "fixed_text": {
        "label": "One fixed text under every message",
        "default": True,
        "why": "The old send_lines recipe.",
    },
}

# Set by the registry layer at runtime; empty means "nothing overridden".
_FEATURE_OVERRIDES = {}


def _env_feature(name: str):
    raw = os.environ.get(f"TG_FEATURE_{name.upper()}")
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in ("1", "on", "yes", "true", "enable", "enabled")


def feature(name: str) -> bool:
    """Is this feature on? Unknown names are off — a typo must not silently
    enable something."""
    if name not in FEATURES:
        return False
    if name in _FEATURE_OVERRIDES:
        return bool(_FEATURE_OVERRIDES[name])
    from_env = _env_feature(name)
    if from_env is not None:
        return from_env
    return bool(FEATURES[name]["default"])


def feature_state() -> dict:
    """Every feature and where its current value came from — what /features
    prints, and what a dashboard would show."""
    out = {}
    for name in FEATURES:
        if name in _FEATURE_OVERRIDES:
            source = "registry"
        elif _env_feature(name) is not None:
            source = "env"
        else:
            source = "default"
        out[name] = {"on": feature(name), "source": source,
                     **{k: v for k, v in FEATURES[name].items() if k != "default"}}
    return out


SPLIT_LABEL = {"blank": "Blank line", "line": "Per line"}
UNDER_LABEL = {"none": "Nothing", "link": "A link per list", "text": "One fixed text"}


# --------------------------------------------------------------------------- #
# Small persistent registries
#
# Telegram gives a bot no directory: it can only name a chat it has been added
# to or used in, and a person who has pressed Start. So both are remembered the
# moment they are seen, and a chat that has never been seen simply cannot be
# offered — which is honest, and better than a picker full of ids.
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, name: str):
        self.path = DATA_DIR / name
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(str(key), default)

    def put(self, key, value):
        self.data[str(key)] = value
        self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        except Exception as e:
            log.warning("could not save %s: %s", self.path.name, e)


CHATS = None      # chat id -> {"title", "type", "seen"}
PEOPLE = None     # user id -> {"name", "seen"}
PREFS = None      # user id -> {"dest", "split", "under"}   (per PERSON, §4.6.3)


def stores():
    """Open the three registries, once. Lazy rather than created in `build()`
    so that anything touching a preference works in any order — a helper that
    only functions if some other function ran first is a trap for the next
    person, and it made the unit tests crash before it could make the bot."""
    global CHATS, PEOPLE, PREFS
    if PREFS is None:
        CHATS = Store("chats.json")
        PEOPLE = Store("people.json")
        PREFS = Store("split-prefs.json")
    return CHATS, PEOPLE, PREFS


def remember_chat(chat) -> None:
    if not chat or chat.type == ChatType.PRIVATE:
        return
    stores()
    CHATS.put(chat.id, {"title": chat.title or str(chat.id),
                        "type": chat.type, "seen": time.time()})


def remember_person(user) -> None:
    if not user or user.is_bot:
        return
    stores()
    PEOPLE.put(user.id, {"name": label_of(user), "seen": time.time()})


def label_of(user) -> str:
    if not user:
        return "someone"
    if user.username:
        return f"@{user.username}"
    return " ".join(x for x in (user.first_name, user.last_name) if x).strip() or "someone"


def pref(uid: int, key: str, default=None):
    stores()
    return (PREFS.get(uid) or {}).get(key, default)


def set_pref(uid: int, **kw):
    stores()
    cur = dict(PREFS.get(uid) or {})
    cur.update(kw)
    PREFS.put(uid, cur)


# --------------------------------------------------------------------------- #
# Splitting — the two rules, and nothing else
# --------------------------------------------------------------------------- #
# A "blank" line is not always empty. Pasting through a phone keyboard, a note
# app or a browser routinely leaves a non-breaking space, a zero-width space or
# a stray tab on the line that LOOKS blank on screen — and a separator that
# disagrees with what the person can see is the worst kind of bug, because they
# have no way to tell why it did not work.
_INVISIBLE = " \t\u00a0\u1680\u2000-\u200b\u202f\u205f\u2060\u3000\ufeff"
_BLANK_RE = re.compile(rf"\n[{_INVISIBLE}]*\n+")

# A Telegram message that is nothing but a number closes the list being built.
# It is the same marker the bot prints between lists on the way out, which is
# why it needs no explaining: you type what you will see.
# Leading decoration is allowed as well as trailing, so "1", "1.", "1)",
# "- 1 -" and "\u2014 1 \u2014" all read as the same marker. People type what they
# have seen, and the WhatsApp tool printed a bare number while plenty of
# hands add a dash around it.
_DECOR = r"[.)\u2014\u2013\-\s]*"
_MARKER_RE = re.compile(rf"^[{_INVISIBLE}]*{_DECOR}(\d{{1,3}}){_DECOR}[{_INVISIBLE}]*$")


def normalise(text: str) -> str:
    """Line endings from every platform, and invisible characters that make a
    blank line look blank without being blank."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(_INVISIBLE.replace("-", ""))
                     for line in text.split("\n"))


# The words that do what a button does. Kept to a handful and matched only
# against a WHOLE message, exactly like the number marker — anything looser
# would start eating people's content, which is the one thing this bot must
# never do.
# Nothing DESTRUCTIVE is here on purpose. A stray "stop" that threw away a
# 300-message batch would be unforgivable, and /cancel is one keystroke longer.
# "send" is left out for the same reason in reverse: it is an ordinary enough
# word to appear in somebody's content, and it would fire the delivery.
_WORDS = {
    "done": "done", "finish": "done", "finished": "done",
    "close": "close", "end": "close",
}


def is_word(text: str):
    """The command a lone word means, or None. `done`, `Done`, `DONE.` all
    read the same; `done deal` is content."""
    if not feature("text_commands"):
        return None
    cleaned = normalise(text).strip().strip(".!,: ").lower()
    return _WORDS.get(cleaned)


def is_marker(text: str):
    """The number in a lone-number message, or None. Only a message that is
    ENTIRELY a number counts — so a line reading "1" inside a pasted block is
    content, which is what makes the rule safe to state in one sentence."""
    m = _MARKER_RE.match(normalise(text).strip())
    return int(m.group(1)) if m else None


def split_blank(text: str) -> list:
    """One pasted block -> messages, separated by a blank line. The WhatsApp
    convention colleagues already type."""
    text = normalise(text)
    return [p.strip() for p in _BLANK_RE.split(text.strip()) if p.strip()]


def split_lines(text: str) -> list:
    """Every non-empty line becomes its own message — the `send_lines` recipe,
    which is what a list of links wants."""
    return [ln.strip() for ln in normalise(text).splitlines() if ln.strip()]


def split_with(rule: str, text: str) -> list:
    return split_lines(text) if rule == "line" else split_blank(text)


def compose(message: str, under: str, link: str = "", fixed: str = "") -> str:
    """One message as it will actually be sent, with whatever goes under it.

    Truncation is refused rather than silently applied: a message cut in half
    on delivery is worse than one that was never sent, because nobody notices.
    """
    tail = link if under == "link" else (fixed if under == "text" else "")
    body = f"{message}\n{tail}" if tail else message
    return body


def too_long(text: str) -> bool:
    return len(text) > MAX_LEN


# --------------------------------------------------------------------------- #
# Session state — per PERSON, because the composing happens in their DM
# --------------------------------------------------------------------------- #
S = {}            # user id -> session
LAST_SHOWN = {}   # (chat, message) -> what we last wrote there


def new_session(uid: int) -> dict:
    return {"state": "idle",
            "split": pref(uid, "split", "blank"),
            "under": pref(uid, "under", "none"),
            "dest": pref(uid, "dest") or {"kind": "dm", "id": uid, "title": "your DM"},
            # [{"messages": [...], "link": str|None, "open": bool}] — the last
            # list stays OPEN and every message joins it until a lone number
            # closes it. Telegram caps one message at 4096 characters, so a
            # long list simply cannot arrive in one paste; making a list span
            # several messages is what lets it arrive at all.
            "lists": [],
            "fixed": "",            # the one text appended under every message
            "origin": None,         # the group this was started from, if any
            "panel": None, "task": None, "sending": False, "cancel": False,
            "sent": [],             # (chat_id, message_id) for Undo
            "sent_at": 0.0}


def sess(uid: int) -> dict:
    return S.setdefault(uid, new_session(uid))


def total_of(s: dict) -> int:
    return sum(len(l["messages"]) for l in s["lists"])


def open_list(s: dict):
    """The list currently being built, or None."""
    return s["lists"][-1] if s["lists"] and s["lists"][-1].get("open") else None


def start_list(s: dict) -> dict:
    l = {"messages": [], "link": None, "open": True}
    s["lists"].append(l)
    return l


def close_list(s: dict) -> bool:
    """Finish the open list. An empty one is dropped rather than closed: two
    numbers in a row should not produce a list with nothing in it."""
    l = open_list(s)
    if l is None:
        return False
    if not l["messages"]:
        s["lists"].pop()
        return False
    l["open"] = False
    return True


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
def allowed(update: Update) -> bool:
    user = update.effective_user
    return not ALLOWED or (user and user.id in ALLOWED)


async def may_send_elsewhere(ctx, uid: int) -> bool:
    """`send.other_chat` — the only scope that can push messages into somebody
    else's chat. With a Report Maker token the registry decides; without one it
    falls back to a local allowlist, which is the same deliberate stopgap the
    report bot shipped with.
    """
    api: TokenClient = ctx.application.bot_data.get("api")
    if not api:
        return not SEND_OTHER or uid in SEND_OTHER
    try:
        me = await api.whoami(actor=f"tg:{uid}")
    except ApiError as e:
        log.warning("scope check failed (%s) — refusing", e)
        return False
    return "send.other_chat" in (me.get("scopes") or [])


# --------------------------------------------------------------------------- #
# The panel — one message per session, rewritten in place
# --------------------------------------------------------------------------- #
async def panel(ctx, uid: int, text: str, kb=None, fresh: bool = False):
    s = sess(uid)
    key = s.get("panel")
    sig = (text, str(kb))
    if key and not fresh and LAST_SHOWN.get(key) == sig:
        return                       # Telegram answers 400 for an identical edit

    async def send():
        m = await ctx.bot.send_message(uid, text, parse_mode=ParseMode.HTML,
                                       reply_markup=kb,
                                       disable_web_page_preview=True)
        s["panel"] = (uid, m.message_id)
        LAST_SHOWN[s["panel"]] = sig

    if not key or fresh:
        return await send()
    try:
        await ctx.bot.edit_message_text(text, chat_id=key[0], message_id=key[1],
                                        parse_mode=ParseMode.HTML, reply_markup=kb,
                                        disable_web_page_preview=True)
        LAST_SHOWN[key] = sig
    except BadRequest as e:
        said = str(e).lower()
        if "not modified" in said:
            LAST_SHOWN[key] = sig
            return
        if "parse" in said or "entit" in said:
            await ctx.bot.edit_message_text(re.sub(r"<[^>]+>", "", text),
                                            chat_id=key[0], message_id=key[1],
                                            reply_markup=kb)
            LAST_SHOWN[key] = sig
            return
        log.warning("panel edit failed (%s) — starting a new one", e)
        await send()


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


def row(label: str, value: str) -> str:
    return f"<b>{label}</b>   {html.escape(value)}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #
async def screen_setup(ctx, uid: int, fresh: bool = False):
    s = sess(uid)
    s["state"] = "setup"
    await panel(ctx, uid,
                "<b>New batch</b>\n\n"
                + row("Split", SPLIT_LABEL[s["split"]]) + "\n"
                + row("Under each", UNDER_LABEL[s["under"]]) + "\n"
                + row("Send to", s["dest"]["title"]),
                kb([("Split", "pick:split"), ("Under each", "pick:under")],
                   [("Send to", "pick:dest")],
                   [("Continue", "go")]), fresh=fresh)


async def screen_split(ctx, uid: int):
    s = sess(uid)
    await panel(ctx, uid,
                "<b>How should a pasted block become messages?</b>\n"
                "<i>Blank line — one blank line between messages, the way the "
                "old tool worked.\nPer line — every line is its own message, "
                "for a list of links.</i>",
                kb([(("• " if s["split"] == "blank" else "") + "Blank line", "split:blank")],
                   [(("• " if s["split"] == "line" else "") + "Per line", "split:line")],
                   [("Back", "back")]))


async def screen_under(ctx, uid: int):
    s = sess(uid)
    await panel(ctx, uid,
                "<b>What goes under every message?</b>\n"
                "<i>A link per list — you send each list, then its link.\n"
                "One fixed text — the same line under every message in the "
                "batch.</i>",
                kb([(("• " if s["under"] == "none" else "") + "Nothing", "under:none")],
                   [(("• " if s["under"] == "link" else "") + "A link per list", "under:link")],
                   *([[(("• " if s["under"] == "text" else "")
                        + "One fixed text", "under:text")]]
                     if feature("fixed_text") else []),
                   [("Back", "back")]))


async def screen_dest(ctx, uid: int):
    s = sess(uid)
    rows = [[("• " if s["dest"]["kind"] == "dm" else "") + "My DM", "dest:dm"]]
    if s.get("origin"):
        rows.append([("• " if s["dest"].get("id") == s["origin"]["id"] else "")
                     + s["origin"]["title"], "dest:origin"])
    rows.append(["Another chat…", "dest:other"])
    rows.append(["Back", "back"])
    await panel(ctx, uid, "<b>Where should the messages go?</b>",
                kb(*[[(t, d)] for t, d in rows]))


async def screen_other(ctx, uid: int):
    """The picker, built from the two registries. A chat the bot has never seen
    cannot be named — Telegram offers no directory, and inventing one would
    mean guessing."""
    if not await may_send_elsewhere(ctx, uid):
        return await panel(ctx, uid,
                           "<b>Sending into other people's chats is not enabled "
                           "for you.</b>\n<i>It is a separate permission "
                           "(<code>send.other_chat</code>) because it is the "
                           "only one that puts messages where the sender is not "
                           "standing. An administrator can grant it.</i>",
                           kb([("Back", "pick:dest")]))
    s = sess(uid)
    rows, options = [], []
    stores()
    for cid, info in sorted(CHATS.data.items(),
                            key=lambda kv: -(kv[1].get("seen") or 0))[:12]:
        options.append({"kind": "chat", "id": int(cid), "title": info["title"]})
    for pid, info in sorted(PEOPLE.data.items(),
                            key=lambda kv: -(kv[1].get("seen") or 0))[:12]:
        if int(pid) == uid:
            continue
        options.append({"kind": "person", "id": int(pid), "title": info["name"]})
    s["_options"] = options
    if not options:
        return await panel(ctx, uid,
                           "<b>No other chats to offer yet.</b>\n"
                           "<i>Telegram gives a bot no directory: it can only "
                           "name a group it has been added to, and a person who "
                           "has pressed Start. Add it to the group, or ask them "
                           "to start it.</i>",
                           kb([("Back", "pick:dest")]))
    for i, o in enumerate(options):
        mark = "· " if o["kind"] == "person" else ""
        rows.append([(mark + o["title"][:56], f"dest:o:{i}")])
    rows.append([("Back", "pick:dest")])
    await panel(ctx, uid,
                "<b>Which chat?</b>\n<i>Groups the bot is in, and people who "
                "have started it.</i>", kb(*rows))


async def screen_collect(ctx, uid: int):
    s = sess(uid)
    s["state"] = "collecting"
    if s["under"] == "text" and not s["fixed"]:
        s["state"] = "fixed"
        return await panel(ctx, uid,
                           "<b>Send the line that goes under every message.</b>",
                           kb([("Back", "back")]))
    inside = ("a blank line starts a new message"
              if s["split"] == "blank" else
              "every line becomes its own message")
    hint = (f"Send as many messages as you like — inside each one, {inside}. "
            "They all join the list you are building.\n"
            "Send <b>1</b> on its own to close that list and start the next.")
    if feature("text_commands"):
        hint += " Send <b>done</b> when the whole batch is ready."
    if s["under"] == "link":
        hint += "\nThe list's link can go in any time before you close it."
    body = f"<b>Send your lists.</b>\n<i>{hint}</i>"
    if s["lists"]:
        body += "\n\n" + list_summary(s)
    if not s["lists"]:
        return await panel(ctx, uid, body, kb([("Cancel", "reset")]))
    buttons = [[("Done", "done")]]
    if open_list(s):
        # The lone number is the fast path; the button is how somebody finds
        # out that closing a list is a thing at all.
        buttons.insert(0, [("Close this list", "close")])
    buttons.append([("Start over", "reset")])
    await panel(ctx, uid, body, kb(*buttons))


def list_summary(s: dict) -> str:
    out = []
    for i, l in enumerate(s["lists"], 1):
        extra = ""
        if s["under"] == "link":
            extra = " · link ✓" if l["link"] else " · <i>link missing</i>"
        state = " · <i>still open</i>" if l.get("open") else ""
        out.append(f"<b>{i}.</b> {plural(len(l['messages']), 'message')}"
                   f"{extra}{state}")
    return "\n".join(out)


async def screen_card(ctx, uid: int):
    s = sess(uid)
    s["state"] = "confirm"
    total, lists = total_of(s), len(s["lists"])
    missing = [i for i, l in enumerate(s["lists"], 1)
               if s["under"] == "link" and not l["link"]]
    long_ones = [i for i, l in enumerate(s["lists"], 1)
                 if any(too_long(compose(m, s["under"], l["link"] or "", s["fixed"]))
                        for m in l["messages"])]

    body = ("<b>Ready to send</b>\n\n"
            + row("Messages", str(total)) + "\n"
            + row("Lists", str(lists)) + "\n"
            + row("Split", SPLIT_LABEL[s["split"]]) + "\n"
            + row("Under each", UNDER_LABEL[s["under"]]
                  + (f" — {s['fixed'][:40]}" if s["under"] == "text" else "")) + "\n"
            + row("Send to", s["dest"]["title"]) + "\n"
            + row("Time", pace_text(s)))
    if missing:
        body += (f"\n\n<i>List {', '.join(map(str, missing))} still needs its "
                 "link.</i>")
    if long_ones:
        body += (f"\n\n<i>List {', '.join(map(str, long_ones))} has a message "
                 f"over Telegram's {MAX_LEN}-character limit. Shorten it — a "
                 "message cut in half on delivery is worse than one never "
                 "sent.</i>")
    buttons = [[("Preview", "preview"), ("Edit", "edit")]] if feature("preview") \
        else [[("Edit", "edit")]]
    if not missing and not long_ones:
        buttons.insert(0, [("Send", "send")])
    await panel(ctx, uid, body, kb(*buttons))


def interval_for(kind: str) -> float:
    return DM_INTERVAL if kind in ("dm", "person") else GROUP_INTERVAL


def pace_text(s: dict) -> str:
    """Honest, because the number is a consequence of Telegram's limit and not
    of anything this bot could hurry."""
    gap = interval_for(s["dest"]["kind"])
    seconds = (total_of(s) + len(s["lists"])) * gap
    if seconds < 75:
        return f"about {max(5, int(round(seconds / 5)) * 5)} seconds"
    return f"about {round(seconds / 60)} minutes"


async def screen_preview(ctx, uid: int):
    s = sess(uid)
    lines = []
    for i, l in enumerate(s["lists"], 1):
        for m in l["messages"][:3]:
            text = compose(m, s["under"], l["link"] or "", s["fixed"])
            lines.append("• " + html.escape(text[:160].replace("\n", " ⏎ ")))
        if len(l["messages"]) > 3:
            lines.append(f"  <i>… and {len(l['messages']) - 3} more</i>")
        lines.append(f"<b>{i}</b>")
    shown = "\n".join(lines[:40])
    await panel(ctx, uid,
                f"<b>What will land in {html.escape(s['dest']['title'])}</b>\n"
                f"<i>One bullet per message; the bold number is the marker sent "
                f"after each list.</i>\n\n{shown}",
                kb([("Send", "send")], [("Edit", "edit")]))


async def screen_edit(ctx, uid: int):
    await panel(ctx, uid, "<b>What needs changing?</b>",
                kb([("Split", "pick:split"), ("Under each", "pick:under")],
                   [("Destination", "pick:dest"), ("Add a list", "more")],
                   [("Drop the last list", "drop"), ("Start over", "reset")],
                   [("Back", "back")]))


# --------------------------------------------------------------------------- #
# Sending — paced, interruptible, and honest about where it got to
# --------------------------------------------------------------------------- #
def bar(done: int, total: int, width: int = 16) -> str:
    filled = 0 if not total else max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


async def deliver(ctx, uid: int, who: str):
    s = sess(uid)
    dest = s["dest"]
    gap = interval_for(dest["kind"])
    total = total_of(s) + len(s["lists"])          # markers count against the rate
    s["sending"], s["cancel"], s["sent"] = True, False, []
    done = 0
    failed = None

    async def put(text: str, as_html: bool = False) -> bool:
        """One message, retried once through Telegram's own flood wait.

        A RetryAfter mid-list is exactly the case pacing exists to avoid; when
        it happens anyway, waiting it out is right — abandoning the list would
        leave half a numbered block in somebody's chat.
        """
        nonlocal failed
        for attempt in (1, 2):
            try:
                m = await ctx.bot.send_message(
                    dest["id"], text,
                    parse_mode=ParseMode.HTML if as_html else None,
                    disable_web_page_preview=True)
                s["sent"].append((dest["id"], m.message_id))
                return True
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 5)) + 1
                log.warning("flood wait %.0fs", wait)
                await asyncio.sleep(wait)
            except Forbidden:
                failed = (
                    "Telegram will not let a bot open a conversation. "
                    + (f"Ask {html.escape(dest['title'])} to press Start on this "
                       f"bot first — then it can send to them."
                       if dest["kind"] == "person" else
                       "The bot may have been removed from that chat."))
                return False
            except TelegramError as e:
                failed = f"Telegram refused: {html.escape(str(e))}"
                return False
        failed = "Telegram kept asking to wait. Nothing more was sent."
        return False

    # Accountability goes in ONE line before the batch, not onto every message.
    # These messages are meant to land exactly as they were written — appending
    # "sent by @tilak" to each of sixty of them would quietly rewrite the very
    # content this bot exists to deliver verbatim.
    if dest["kind"] in ("chat", "person"):
        await put(f"<i>Sent by {html.escape(who)}</i>", as_html=True)
        await asyncio.sleep(gap)

    for i, l in enumerate(s["lists"], 1):
        for msg in l["messages"]:
            if s["cancel"]:
                break
            body = compose(msg, s["under"], l["link"] or "", s["fixed"])
            ok = await put(body)
            if not ok:
                break
            done += 1
            if done % 3 == 0 or done == total:
                await panel(ctx, uid,
                            f"<b>Sending to {html.escape(dest['title'])}</b>\n"
                            f"<code>{bar(done, total)}</code>  "
                            f"{done} of {total}\n"
                            f"<i>List {i} of {len(s['lists'])}</i>",
                            kb([("Stop", "stop")]))
            await asyncio.sleep(gap)
        if failed or s["cancel"]:
            break
        if not await put(str(i)):
            break
        done += 1
        await asyncio.sleep(gap)

    s["sending"] = False
    s["sent_at"] = time.time()
    sent_msgs = len([1 for _ in s["sent"]])
    lists_done = i if (failed or s["cancel"]) else len(s["lists"])

    if failed:
        head = (f"<b>Stopped after {plural(sent_msgs, 'message')}</b>\n{failed}")
    elif s["cancel"]:
        head = (f"<b>Stopped at your request</b>\n"
                f"{plural(sent_msgs, 'message')} had already gone into "
                f"{html.escape(dest['title'])}.")
    else:
        head = (f"<b>Sent {plural(total_of(s), 'message')} in "
                f"{plural(len(s['lists']), 'list')}</b>\n"
                f"to {html.escape(dest['title'])}.")
    rows = [[("New batch", "restart")]]
    if feature("undo") and s["sent"]:
        rows.insert(0, [("Undo — delete them", "undo")])
    await panel(ctx, uid, head, kb(*rows), fresh=True)

    # A receipt where you are standing, when the delivery went somewhere else.
    # Nobody should have to switch chats to check that it worked (§4.4).
    if dest["kind"] != "dm" and not failed and feature("receipt"):
        await ctx.bot.send_message(
            uid, f"Sent {plural(sent_msgs, 'message')} in "
                 f"{plural(lists_done, 'list')} to "
                 f"{html.escape(dest['title'])}.",
            parse_mode=ParseMode.HTML)


async def undo(ctx, uid: int):
    if not feature("undo"):
        return await panel(ctx, uid, "<b>Undo is switched off.</b>",
                           kb([("New batch", "restart")]))
    """Delete what was just sent. New with Telegram and worth using: on
    WhatsApp a wrong list was simply in the chat for ever."""
    s = sess(uid)
    if not s["sent"]:
        return await panel(ctx, uid, "<b>Nothing to undo.</b>",
                           kb([("New batch", "restart")]))
    if time.time() - s["sent_at"] > UNDO_MINUTES * 60:
        return await panel(ctx, uid,
                           f"<b>Too late to undo.</b>\n<i>Undo is offered for "
                           f"{int(UNDO_MINUTES)} minutes after sending.</i>",
                           kb([("New batch", "restart")]))
    gone = 0
    for chat_id, mid in s["sent"]:
        try:
            await ctx.bot.delete_message(chat_id, mid)
            gone += 1
        except TelegramError:
            pass
    s["sent"] = []
    await panel(ctx, uid,
                f"<b>Deleted {plural(gone, 'message')}.</b>"
                + ("" if gone == len(s['sent']) else
                   "\n<i>Anything Telegram would not delete is still there.</i>"),
                kb([("New batch", "restart")]), fresh=True)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not allowed(update):
        return await update.message.reply_text(
            "<b>This bot is private.</b>\nAsk an administrator to add you. Your "
            f"Telegram id is <code>{user.id}</code>.", parse_mode=ParseMode.HTML)
    remember_person(user)
    if update.effective_chat.type != ChatType.PRIVATE:
        return await offer_dm(update, ctx)
    S[user.id] = new_session(user.id)
    await ctx.bot.send_message(
        user.id,
        "<b>Message splitter</b>\n"
        "Send it a batch of lists; it puts every message of list 1 into a chat "
        "one at a time, then <b>1</b>, then list 2, then <b>2</b>, and so on.\n\n"
        "<i>Tag me in a group and we'll carry on here.</i>",
        parse_mode=ParseMode.HTML)
    await screen_setup(ctx, user.id, fresh=True)


async def offer_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """One line in the group, everything else in the DM (§4.2).

    The button is also the deep link that fixes the never-started-the-bot case,
    sitting exactly where people will naturally tap it.
    """
    chat, user = update.effective_chat, update.effective_user
    remember_chat(chat)
    remember_person(user)
    s = sess(user.id)
    s["origin"] = {"id": chat.id, "title": chat.title or "this group"}
    link = f"https://t.me/{BOT_USERNAME}?start=go" if BOT_USERNAME else ""
    markup = (InlineKeyboardMarkup([[InlineKeyboardButton("Continue in your DM ↗",
                                                          url=link)]])
              if link else None)
    note = await update.message.reply_text("Continue in your DM ↗",
                                           reply_markup=markup)
    try:
        S[user.id] = dict(new_session(user.id), origin=s["origin"])
        await screen_setup(ctx, user.id, fresh=True)
    except Forbidden:
        # They have never started the bot, so the DM cannot be opened. The
        # button above is the fix; say so instead of failing silently.
        await note.edit_text(
            "Tap the button, press Start, and we'll carry on there.",
            reply_markup=markup)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    s = sess(uid)
    if s.get("sending"):
        s["cancel"] = True
        return
    S[uid] = new_session(uid)
    await ctx.bot.send_message(uid, "Discarded.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    uid = update.effective_user.id
    s = sess(uid)
    if s["state"] == "idle":
        return await ctx.bot.send_message(uid, "Nothing in progress. /start to begin.")
    await panel(ctx, uid,
                f"<b>{plural(total_of(s), 'message')} in "
                f"{plural(len(s['lists']), 'list')}</b>\n"
                + row("Send to", s["dest"]["title"]) + "\n\n" + list_summary(s),
                kb([("Done", "done"), ("Start over", "reset")]), fresh=True)


async def cmd_features(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """What is switched on, and where each answer came from. A flag nobody can
    read is worse than no flag: the first question when something is missing is
    always "is it off, or is it broken?"."""
    if not allowed(update):
        return
    lines = ["<b>Features</b>"]
    for name, info in feature_state().items():
        mark = "on " if info["on"] else "off"
        lines.append(f"<code>{mark}</code>  {html.escape(info['label'])} "
                     f"<i>({info['source']})</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pull the switches from the dashboard now instead of waiting for the next
    refresh — the thing anybody wants immediately after flipping one."""
    if not allowed(update):
        return
    if not ctx.application.bot_data.get("api"):
        return await update.message.reply_text(
            "Not connected to Report Maker, so features come from this bot's "
            "own .env only.")
    await sync_features(ctx.application)
    await cmd_features(update, ctx)


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"Your Telegram id is {u.id}.\n"
        f"This chat's id is {update.effective_chat.id}.")


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/link 482913` — bind this Telegram id to a Report Maker account with a
    one-shot code an admin generated. The bot never sees a password."""
    api: TokenClient = ctx.application.bot_data.get("api")
    uid = update.effective_user.id
    if not api:
        return await update.message.reply_text(
            "This bot is not connected to Report Maker, so there is nothing to "
            "link to.")
    code = (ctx.args[0] if ctx.args else "").strip()
    if not code:
        return await update.message.reply_text(
            "Send /link followed by the code an administrator gave you.")
    try:
        out = await api.link(code, actor=f"tg:{uid}")
    except ApiError as e:
        return await update.message.reply_text(str(e))
    await update.message.reply_text(f"Linked. You act as {out.get('user')}.")


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def addressed_to_us(update: Update) -> bool:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    msg = update.effective_message
    body = msg.text or msg.caption or ""
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in body.lower():
        return True
    reply = msg.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.is_bot
                and reply.from_user.username == BOT_USERNAME)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat, user = update.effective_chat, update.effective_user
    remember_person(user)
    if chat.type != ChatType.PRIVATE:
        remember_chat(chat)
        if addressed_to_us(update):
            await offer_dm(update, ctx)
        return                                   # groups never collect content

    uid = user.id
    s = sess(uid)
    body = update.message.text or ""

    if s["state"] == "fixed":
        s["fixed"] = body.strip()[:400]
        return await screen_collect(ctx, uid)

    if s["state"] not in ("collecting", "confirm", "setup"):
        return await screen_setup(ctx, uid, fresh=True)
    if s["state"] != "collecting":
        s["state"] = "collecting"

    # A message that is only a word does what the matching button does. Typing
    # is faster than reaching for a button when your hands are already in the
    # message box, and both stay available.
    word = is_word(body)
    if word == "done":
        close_list(s)
        if not s["lists"]:
            return await screen_collect(ctx, uid)
        return await screen_card(ctx, uid)
    if word == "close":
        if not close_list(s):
            return await panel(ctx, uid,
                               "<b>Nothing open to close.</b>\n<i>Send the "
                               "messages first.</i>",
                               kb([("Done", "done"), ("Start over", "reset")])
                               if s["lists"] else kb([("Cancel", "reset")]))
        return await screen_collect(ctx, uid)
    # A message that is only a number closes the list being built. Same marker
    # the bot prints between lists on the way out, so there is nothing to learn.
    marker = is_marker(body)
    if marker is not None:
        if not close_list(s):
            return await panel(ctx, uid,
                               "<b>Nothing open to close.</b>\n<i>Send the "
                               "messages first, then a number.</i>",
                               kb([("Done", "done"), ("Start over", "reset")])
                               if s["lists"] else kb([("Cancel", "reset")]))
        return await screen_collect(ctx, uid)

    # A link on its own, when links are what goes under each message, belongs
    # to the list being built — not to a new one.
    if s["under"] == "link" and len(body.split()) == 1 and \
            body.strip().lower().startswith(("http://", "https://")):
        target = open_list(s) or (s["lists"][-1] if s["lists"] else None)
        if target is None or target["link"] is not None:
            return await panel(ctx, uid,
                               "<b>Send the list first, then its link.</b>",
                               kb([("Done", "done"), ("Start over", "reset")])
                               if s["lists"] else kb([("Cancel", "reset")]))
        target["link"] = body.strip()
        return await screen_collect(ctx, uid)

    messages = split_with(s["split"], body)
    if not messages:
        return
    if total_of(s) + len(messages) > MAX_MESSAGES:
        return await panel(ctx, uid,
                           f"<b>That would be over {MAX_MESSAGES} messages.</b>\n"
                           "<i>Send this batch, then start another.</i>",
                           kb([("Done", "done"), ("Start over", "reset")]))
    # Joins the list being built, opening one if this is the first message.
    # Telegram's 4096-character ceiling means a long list arrives in pieces;
    # treating each piece as its own list is what made that unusable.
    (open_list(s) or start_list(s))["messages"].extend(messages)
    await screen_collect(ctx, uid)


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    uid = update.effective_user.id
    s = sess(uid)
    data = q.data or ""

    try:
        if data == "go":
            return await screen_collect(ctx, uid)
        if data == "back":
            return await (screen_card(ctx, uid) if s["lists"]
                          else screen_setup(ctx, uid))
        if data == "restart" or data == "reset":
            origin = s.get("origin")
            S[uid] = new_session(uid)
            S[uid]["origin"] = origin
            return await screen_setup(ctx, uid, fresh=True)

        if data == "pick:split":
            return await screen_split(ctx, uid)
        if data == "pick:under":
            return await screen_under(ctx, uid)
        if data == "pick:dest":
            return await screen_dest(ctx, uid)
        if data == "dest:other":
            return await screen_other(ctx, uid)

        if data.startswith("split:"):
            s["split"] = data.split(":", 1)[1]
            set_pref(uid, split=s["split"])
            # Re-split what is already collected: the rule is a property of the
            # batch, and leaving earlier lists cut the old way would send a
            # batch split two different ways without saying so.
            for l in s["lists"]:
                l["messages"] = split_with(s["split"], "\n\n".join(l["messages"]))
            if open_list(s) and not s["lists"][-1]["messages"]:
                s["lists"].pop()
            return await (screen_card(ctx, uid) if s["lists"] else screen_setup(ctx, uid))
        if data.startswith("under:"):
            s["under"] = data.split(":", 1)[1]
            set_pref(uid, under=s["under"])
            if s["under"] == "text" and not s["fixed"]:
                s["state"] = "fixed"
                return await panel(ctx, uid,
                                   "<b>Send the line that goes under every "
                                   "message.</b>", kb([("Back", "back")]))
            return await (screen_card(ctx, uid) if s["lists"] else screen_setup(ctx, uid))

        if data == "dest:dm":
            s["dest"] = {"kind": "dm", "id": uid, "title": "your DM"}
        elif data == "dest:origin" and s.get("origin"):
            s["dest"] = {"kind": "chat", **s["origin"]}
        elif data.startswith("dest:o:"):
            chosen = (s.get("_options") or [])[int(data.rsplit(":", 1)[1])]
            if not await may_send_elsewhere(ctx, uid):
                return await screen_other(ctx, uid)
            s["dest"] = dict(chosen)
        if data.startswith("dest:"):
            set_pref(uid, dest=s["dest"])          # per PERSON (§4.6.3)
            return await (screen_card(ctx, uid) if s["lists"] else screen_setup(ctx, uid))

        if data == "close":
            close_list(s)
            return await screen_collect(ctx, uid)
        if data == "done":
            # Whatever is still open is finished here, so nobody has to send a
            # trailing number just to be allowed to press the button.
            close_list(s)
            if not s["lists"]:
                return await screen_collect(ctx, uid)
            return await screen_card(ctx, uid)
        if data == "more":
            # The open list was closed by Done, so the next message starts a
            # new one rather than joining the batch that is already priced.
            return await screen_collect(ctx, uid)
        if data == "drop":
            if s["lists"]:
                s["lists"].pop()
            return await (screen_card(ctx, uid) if s["lists"] else screen_collect(ctx, uid))
        if data == "edit":
            return await screen_edit(ctx, uid)
        if data == "preview":
            # Reachable from an old panel after the feature was switched off.
            if not feature("preview"):
                return await screen_card(ctx, uid)
            return await screen_preview(ctx, uid)

        if data == "send":
            if s.get("sending"):
                return
            who = label_of(update.effective_user)
            if s["dest"]["kind"] in ("chat", "person") and \
                    not await may_send_elsewhere(ctx, uid):
                return await screen_other(ctx, uid)
            return await deliver(ctx, uid, who)
        if data == "stop":
            s["cancel"] = True
            return
        if data == "undo":
            return await undo(ctx, uid)
    except Forbidden as e:
        await ctx.bot.send_message(
            uid, f"Telegram refused: {e}. A bot cannot open a conversation — "
                 "the other person has to press Start first.")
    except TelegramError as e:
        log.warning("button %s failed: %s", data, e)


async def on_added(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remember every chat the bot is put into — the only way it can ever name
    one later."""
    remember_chat(update.effective_chat)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
FEATURE_REFRESH_SECONDS = float(os.environ.get("TG_FEATURE_REFRESH", "300") or 300)


def _catalogue() -> list:
    return [{"name": name, "label": info["label"], "why": info["why"],
             "default": info["default"]} for name, info in FEATURES.items()]


async def sync_features(app: Application, first: bool = False) -> None:
    """Announce what this bot can do, and take back the admin's answers.

    A failure here is logged and otherwise ignored: the env and default layers
    still resolve, so an unreachable dashboard slows nobody down. A bot that
    refused to work because a config server was down would be a worse bot.
    """
    api = app.bot_data.get("api")
    if not api:
        return
    global _FEATURE_OVERRIDES
    try:
        got = (await api.announce_features(_catalogue()) if first
               else await api.features())
    except ApiError as e:
        log.warning("feature sync failed (%s) — keeping %s", e,
                    "the announced set" if _FEATURE_OVERRIDES else "env + defaults")
        return
    # Only names this build actually knows. A stale row for a feature that has
    # since been removed must not resurrect a switch that controls nothing.
    _FEATURE_OVERRIDES = {k: bool(v) for k, v in (got or {}).items() if k in FEATURES}
    log.info("features from the registry: %s",
             ", ".join(f"{k}={'on' if v else 'off'}"
                       for k, v in sorted(_FEATURE_OVERRIDES.items())) or "none")


async def _feature_loop(app: Application):
    """A plain asyncio task rather than PTB's JobQueue, which needs the
    [job-queue] extra (APScheduler) that this deliberately tiny image does not
    install. One sleep and one call does not justify a dependency."""
    while True:
        try:
            await asyncio.sleep(FEATURE_REFRESH_SECONDS)
            await sync_features(app)
        except asyncio.CancelledError:
            return
        except Exception as e:                       # never kill the loop
            log.warning("feature refresh: %s", e)


async def _post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("splitter online as @%s", BOT_USERNAME)
    await sync_features(app, first=True)
    if app.bot_data.get("api"):
        app.bot_data["feature_task"] = asyncio.create_task(_feature_loop(app))


async def _post_shutdown(app: Application):
    task = app.bot_data.get("feature_task")
    if task and not task.done():
        task.cancel()


def build() -> Application:
    stores()
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    if RM_TOKEN and APP_URL:
        app.bot_data["api"] = TokenClient(APP_URL, RM_TOKEN)
        log.info("scopes come from %s", APP_URL)
    else:
        log.info("no Report Maker token — send.other_chat falls back to "
                 "TG_SEND_OTHER_IDS")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("features", cmd_features))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_added))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Set TG_SPLITTER_TOKEN (see tg/.env.splitter.example).")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    build().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

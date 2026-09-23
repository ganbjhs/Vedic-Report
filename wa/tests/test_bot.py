"""
Bot loop tests — no browser. `bot.read_visible`, `bot.send_text` and
`bot.open_chat` are swapped for a scripted fake chat, so each test plays a
sequence of polls and checks what the bot said and what it kept.

    python3 -m pytest wa/tests -q        (from the wa/ folder: python3 -m pytest tests -q)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# No browser here: give wa.session an import-able stand-in for playwright so
# the tests run on a machine without it installed.
try:
    import playwright.sync_api  # noqa: F401
except ImportError:
    import types
    _pw = types.ModuleType("playwright"); _api = types.ModuleType("playwright.sync_api")
    _api.sync_playwright = lambda: None; _api.Page = object; _api.BrowserContext = object
    _pw.sync_api = _api
    sys.modules["playwright"] = _pw; sys.modules["playwright.sync_api"] = _api

import bot as botmod  # noqa: E402
from bot import BOT, Bot  # noqa: E402


class FakeChat:
    """The visible window of a group chat. `add()` appends a message; the bot's
    own replies are appended as outgoing (so ignore_own has something to ignore)."""

    def __init__(self, name="Testing"):
        self.name = name
        self.msgs: list[dict] = []
        self.sent: list[str] = []
        self.n = 0
        self.fail_next_send = 0
        self.opened: list[str] = []

    def add(self, text, sender="Rahul", outgoing=False, with_id=True, when="10:00", truncated=False):
        self.n += 1
        self.msgs.append({"id": f"false_g@g.us_{self.n:04d}" if with_id else None,
                          "sender": sender, "phone": "", "time": when, "text": text,
                          "links": [], "outgoing": outgoing, "truncated": truncated})
        return self.msgs[-1]

    # ---- the three functions bot.py imports
    def read_visible(self, s, expand=True):
        return list(self.msgs)

    def send_text(self, s, text, delay_after=0.0):
        if self.fail_next_send:
            self.fail_next_send -= 1
            raise TimeoutError("composer covered by a tooltip")
        self.sent.append(text)
        self.add(text, sender="me", outgoing=True)

    def open_chat(self, s, name):
        self.opened.append(name)
        self.name = name


class FakeSession:
    def __init__(self, chat: FakeChat, header=None):
        self.chat = chat
        self.header = header

    def current_chat(self):
        return self.header if self.header is not None else self.chat.name

    def dismiss_dialogs(self): return True
    def clear_overlays(self): return 0


@pytest.fixture
def world(monkeypatch):
    chat = FakeChat()
    monkeypatch.setattr(botmod, "read_visible", chat.read_visible)
    monkeypatch.setattr(botmod, "send_text", chat.send_text)
    monkeypatch.setattr(botmod, "open_chat", chat.open_chat)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    s = FakeSession(chat)
    b = Bot(s, "Testing", delay=0, poll=0)
    b.snapshot_seen()
    return chat, b


def replies(chat):
    return [t[len(BOT):] for t in chat.sent if t.startswith(BOT)]


# --------------------------------------------------------------- the dropped-command bugs
def test_two_messages_in_one_poll_are_both_handled(world):
    chat, b = world
    chat.add("/start")
    chat.add("1")                       # arrived before the bot polled — used to be the only one seen
    b.poll_once()
    assert b.state == "collect" and b.mode == 1
    assert replies(chat)[:2] == ["New job. Reply 1 for messages only, 2 for messages + link.",
                                 "Send your message list (blank line between messages). Then /run."]


def test_list_and_link_in_one_poll(world):
    chat, b = world
    chat.add("/start"); b.poll_once()
    chat.add("2"); b.poll_once()
    chat.add("hello\n\nworld")
    chat.add("https://x.com/p/1")
    b.poll_once()
    assert b.lists == [{"messages": ["hello", "world"], "link": "https://x.com/p/1"}]


def test_command_during_the_bots_reply_is_not_lost(world):
    chat, b = world
    chat.add("/start")
    b.poll_once()
    # the user's "1" landed while the bot was typing; the bot's reply is newer
    chat.msgs.insert(len(chat.msgs) - 1, {"id": "false_g@g.us_late", "sender": "Rahul", "phone": "",
                                          "time": "10:00", "text": "1", "links": [], "outgoing": False})
    b.poll_once()
    assert b.state == "collect"


def test_repeated_command_without_ids_is_still_a_new_message(world):
    chat, b = world
    chat.add("/status", with_id=False); b.poll_once()
    chat.add("/status", with_id=False); b.poll_once()   # same text, same minute, no id
    assert replies(chat).count("Idle. Send /start to begin.") == 2


def test_failed_reply_is_retried_next_poll(world):
    chat, b = world
    chat.fail_next_send = 2             # send_text raises twice: say()'s own retry, then the poll's
    chat.add("/start")
    with pytest.raises(TimeoutError):
        b.poll_once()
    assert b.state == "mode"            # state moved, reply did not go out
    b.poll_once()                       # same message re-handled; reply lands this time
    assert replies(chat) == ["New job. Reply 1 for messages only, 2 for messages + link."]


def test_own_and_bot_marked_messages_are_ignored(world):
    chat, b = world
    chat.add("/start", sender="me", outgoing=True)
    chat.add(BOT + "/start")
    b.poll_once()
    assert b.state == "idle" and chat.sent == []


def test_burst_is_history_not_commands(world):
    chat, b = world
    for i in range(30):
        chat.add(f"/start {i}")
    b.poll_once()
    assert b.state == "idle" and chat.sent == []


# --------------------------------------------------------------- the added-member bug
def test_other_people_cannot_hijack_an_open_job(world):
    chat, b = world
    chat.add("/start", sender="Rahul"); b.poll_once()
    chat.add("1", sender="Rahul"); b.poll_once()
    chat.add("hi everyone!", sender="New Member"); b.poll_once()
    assert b.lists == []                # the newcomer's chatter is not a list
    chat.add("/cancel", sender="New Member"); b.poll_once()
    assert b.state == "collect"         # nor can they cancel it
    chat.add("/status", sender="New Member"); b.poll_once()
    assert replies(chat)[-1].startswith("Mode: messages only (job by Rahul)")
    chat.add("line one\n\nline two", sender="Rahul"); b.poll_once()
    assert len(b.lists) == 1


def test_job_is_free_again_after_it_ends(world):
    chat, b = world
    chat.add("/start", sender="Rahul"); b.poll_once()
    chat.add("/cancel", sender="Rahul"); b.poll_once()
    chat.add("/start", sender="Priya"); b.poll_once()
    assert b.owner == "Priya"


def test_silent_job_expires(world):
    chat, b = world
    b.job_ttl = 10
    chat.add("/start", sender="Rahul"); b.poll_once()
    b.last_cmd_at -= 11
    b.poll_once()
    assert b.state == "idle" and "expired" in replies(chat)[-1]


# --------------------------------------------------------------- the group-name thrash
def test_header_without_emoji_still_matches_group_with_emoji(world):
    chat, b = world
    b.group = "Bot Control 🤖"
    b.s.header = "Bot Control"
    b.in_group()
    assert chat.opened == []


def test_wrong_chat_is_reopened(world):
    chat, b = world
    b.s.header = "Some Other Chat"
    chat.opened.clear()
    b.in_group()
    assert chat.opened == ["Testing"]


def test_recover_after_repeated_errors(world, monkeypatch):
    chat, b = world
    calls = []
    b.recover = lambda: calls.append(1)
    monkeypatch.setattr(botmod, "read_visible", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    b.max_errors = 2
    for _ in range(2):
        try:
            b.poll_once(); b.errors = 0
        except Exception:
            b.errors += 1
            if b.errors >= b.max_errors:
                b.recover()
    assert calls == [1]


# --------------------------------------------------------------- the short-count bug (long lists)
from bot import split_list  # noqa: E402


def test_blank_lines_with_invisible_characters_still_split():
    text = "one\n\ntwo\n \nthree\n\u00a0\nfour\n\u200b\nfive\n\t\n\nsix"
    assert split_list(text) == ["one", "two", "three", "four", "five", "six"]


def test_single_newlines_do_not_split():
    assert split_list("line a\nline b\n\nline c") == ["line a\nline b", "line c"]


def test_truncated_list_is_not_counted_until_read_more_finishes(world):
    chat, b = world
    chat.add("/start"); b.poll_once()
    chat.add("1"); b.poll_once()
    m = chat.add("\n\n".join(f"msg {i}" for i in range(14)), truncated=True)   # WhatsApp still shows "Read more"
    b.poll_once()
    assert b.lists == []                                # waited, not counted
    m["text"] = "\n\n".join(f"msg {i}" for i in range(20)); m["truncated"] = False
    b.poll_once()
    assert len(b.lists) == 1 and len(b.lists[0]["messages"]) == 20
    assert replies(chat)[-1].startswith("List 1: 20 messages (last: \u201cmsg 19\u2026\u201d)")
    assert "cut this message off" not in replies(chat)[-1]


def test_permanently_truncated_list_is_counted_with_a_warning(world):
    chat, b = world
    chat.add("/start"); b.poll_once()
    chat.add("1"); b.poll_once()
    chat.add("\n\n".join(f"msg {i}" for i in range(14)), truncated=True)
    for _ in range(b.max_wait_polls + 1):
        b.poll_once()
    assert len(b.lists) == 1 and len(b.lists[0]["messages"]) == 14
    assert "cut this message off" in replies(chat)[-1]


def test_a_later_message_waits_behind_a_truncated_one(world):
    chat, b = world
    chat.add("/start"); b.poll_once()
    chat.add("1"); b.poll_once()
    m = chat.add("a\n\nb", truncated=True)
    chat.add("c\n\nd")
    b.poll_once()
    assert b.lists == []                                # order preserved: nothing after the cut one is handled yet
    m["truncated"] = False
    b.poll_once()
    assert [l["messages"] for l in b.lists] == [["a", "b"], ["c", "d"]]

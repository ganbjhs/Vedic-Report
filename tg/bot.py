#!/usr/bin/env python3
"""Report bot — Telegram front door for Report Maker.

The conversation, start to finish:

    /start   ->  Project / Style, with what they are set to now
    Continue ->  "Send the post links."
    links    ->  "12 links received — finished adding?"   [Yes] [Not yet]
    Yes      ->  "Name this report"                       [Use today's date]
    name     ->  the summary: project, style, name, links, time, who asked
                                                          [Run report] [Edit]
    Run      ->  a progress bar, then the PDF, with the other formats on
                 buttons that build on demand

One short question at a time, buttons for the answers, and the whole exchange
lives in a single message that keeps being rewritten. No emoji: this is a tool
colleagues use every week, not a novelty.

Run:  python bot.py
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

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
                      Update)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

try:                                      # `python -m tg.bot`
    from .client import ApiError, ReportMaker, find_links
except ImportError:                       # `python bot.py`
    from client import ApiError, ReportMaker, find_links

log = logging.getLogger("reportbot")
HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _load_env():
    path = HERE / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split("#")[0].strip().strip('"').strip("'"))


_load_env()

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").strip()
APP_USER = os.environ.get("APP_USER", "").strip()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
ALLOWED = {int(x) for x in os.environ.get("TG_ALLOWED_IDS", "").replace(" ", "").split(",")
           if x.strip().isdigit()}
POLL_SECONDS = float(os.environ.get("TG_POLL_SECONDS", "4") or 4)
# How long after the last link before we ask "Done?" — short, because the
# question is cheap and "Not yet" costs one tap.
SETTLE_SECONDS = float(os.environ.get("TG_SETTLE_SECONDS", "3") or 3)

MAX_SEND_BYTES = 45 * 1024 * 1024          # Telegram's document ceiling is 50
MAX_INPUT_BYTES = 5 * 1024 * 1024

FORMAT_LABEL = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint",
                "xlsx": "Excel", "zip": "Screenshots", "csv": "Numbers CSV"}

PREFS_PATH = Path(os.environ.get("TG_DATA_DIR", str(HERE / "data"))) / "prefs.json"
BOT_USERNAME = ""

# All state is keyed by CHAT: in a group the team shares one report.
PREFS: dict = {}        # chat id -> {"project": id, "style": slug}   (persisted)
S: dict = {}            # chat id -> the live session (see new_session)
RUNS: dict = {}         # job id -> {"chat", "sent", "batch"}
LAST: dict = {}         # chat id -> the last job it ran, for /last
LAST_SHOWN: dict = {}   # (chat, message) -> what we last wrote there


def load_prefs():
    global PREFS
    try:
        PREFS = json.loads(PREFS_PATH.read_text())
    except Exception:
        PREFS = {}


def save_prefs():
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(PREFS, indent=1))
    except Exception as e:
        log.warning("could not save prefs: %s", e)


def pref(cid: int, key: str, default=None):
    return (PREFS.get(str(cid)) or {}).get(key, default)


def set_pref(cid: int, **kw):
    PREFS.setdefault(str(cid), {}).update(kw)
    save_prefs()


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def new_session() -> dict:
    return {"state": "idle", "links": [], "seen": set(), "name": "", "file": None,
            "panel": None, "task": None, "project": None, "style": None,
            "count": 0, "dupes": 0, "dropped": 0, "platform": "", "asked": False,
            "by": "", "ran_by": ""}


def sess(cid: int) -> dict:
    return S.setdefault(cid, new_session())


def cancel_settle(s: dict):
    t = s.get("task")
    if t and t is not asyncio.current_task() and not t.done():
        t.cancel()
    s["task"] = None


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
def allowed(update: Update) -> bool:
    """Checked against the PERSON, while state is per chat: in a group only
    listed colleagues drive the bot, but the report belongs to the group."""
    user = update.effective_user
    return not ALLOWED or (user and user.id in ALLOWED)


def cid_of(update: Update) -> int:
    return update.effective_chat.id


def who_of(update: Update) -> str:
    """A human label for the person acting — the report gets attributed to it.

    Prefers @username because it is unique and clickable in a group; falls back
    to the display name, which is all some accounts have.
    """
    u = update.effective_user
    if not u:
        return ""
    if u.username:
        return f"@{u.username}"
    return " ".join(x for x in (u.first_name, u.last_name) if x).strip() or "someone"


def addressed_to_us(update: Update) -> bool:
    """In a group, act only when tagged — or when a batch is already open.

    Requiring @mention on every one of twenty forwards would be absurd, so a
    tag opens the session and the open session keeps it going.
    """
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    msg = update.effective_message
    body = msg.text or msg.caption or ""
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in body.lower():
        return True
    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot \
            and reply.from_user.username == BOT_USERNAME:
        return True
    return S.get(chat.id, {}).get("state", "idle") != "idle"


# --------------------------------------------------------------------------- #
# The panel — one message per session, rewritten in place
# --------------------------------------------------------------------------- #
async def panel(ctx, cid: int, text: str, kb=None, fresh: bool = False):
    """Write `text` into this chat's panel, creating it if needed.

    PTB's Message and CallbackQuery objects are frozen, so the "what did I last
    write" note cannot live on them — it lives in LAST_SHOWN, keyed by message.
    Skipping an identical rewrite matters: Telegram answers 400 for it.
    """
    s = sess(cid)
    key = s.get("panel")
    sig = (text, str(kb))
    if key and not fresh and LAST_SHOWN.get(key) == sig:
        return

    async def send():
        m = await ctx.bot.send_message(cid, text, parse_mode=ParseMode.HTML,
                                       reply_markup=kb)
        s["panel"] = (cid, m.message_id)
        LAST_SHOWN[s["panel"]] = sig

    if not key or fresh:
        return await send()
    try:
        await ctx.bot.edit_message_text(text, chat_id=key[0], message_id=key[1],
                                        parse_mode=ParseMode.HTML, reply_markup=kb)
        LAST_SHOWN[key] = sig
    except BadRequest as e:
        said = str(e).lower()
        if "not modified" in said:
            LAST_SHOWN[key] = sig
            return
        if "parse" in said or "entit" in said:
            log.warning("markup rejected (%s); resending plain", e)
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


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #
def pname(p) -> str:
    return (p or {}).get("name") or "—"


def sname(st) -> str:
    return (st or {}).get("label") or "—"


def duration(seconds: int) -> str:
    """'about 2 minutes' — an estimate should not pretend to be a stopwatch."""
    if seconds < 90:
        return "under a minute"
    return f"about {round(seconds / 60)} minutes"


def elapsed(seconds: int) -> str:
    m, sec = divmod(max(0, int(seconds)), 60)
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def row(label: str, value: str) -> str:
    return f"<b>{label}</b>   {html.escape(value)}"


async def screen_menu(ctx, cid: int, fresh: bool = False):
    s = sess(cid)
    await resolve_into(ctx, cid)
    s["state"] = "idle"
    await panel(ctx, cid,
                "<b>New report</b>\n\n"
                + row("Project", pname(s["project"])) + "\n"
                + row("Style", sname(s["style"])),
                kb([("Change project", "pick:p"), ("Change style", "pick:s")],
                   [("Continue", "go")]), fresh=fresh)


async def screen_ask_links(ctx, cid: int):
    sess(cid)["state"] = "collecting"
    await panel(ctx, cid, "<b>Send the post links.</b>\n"
                          "<i>One message or many — paste, forward or upload a file.</i>")


async def screen_done(ctx, cid: int):
    s = sess(cid)
    s["asked"] = True
    n = len(s["links"])
    await panel(ctx, cid,
                f"<b>{n} link{'s' if n != 1 else ''} received</b>\n"
                "Finished adding?",
                kb([("Yes, continue", "done"), ("Not yet", "wait")]))


async def screen_name(ctx, cid: int):
    sess(cid)["state"] = "naming"
    await panel(ctx, cid, "<b>Name this report</b>\n"
                          "<i>Send it as a message.</i>",
                kb([("Use today's date", "skip")]))


async def screen_card(ctx, cid: int):
    s = s_ = sess(cid)
    s["state"] = "confirm"
    notes = []
    if s["dupes"]:
        notes.append(f"{s['dupes']} duplicate{'s' if s['dupes'] != 1 else ''} removed")
    if s["dropped"]:
        notes.append(f"{s['dropped']} not a {s['platform']} post")
    links = f"{s['count']}" + (f"   <i>{', '.join(notes)}</i>" if notes else "")
    body = ("<b>" + html.escape(s["name"]) + "</b>\n\n"
            + row("Project", pname(s["project"])) + "\n"
            + row("Style", sname(s["style"])) + "\n"
            + f"<b>Links</b>   {links}\n"
            + row("Time", duration(s["count"] * 11)))
    if s.get("by"):
        body += "\n\n<i>Requested by " + html.escape(s["by"]) + "</i>"
    await panel(ctx, cid, body, kb([("Run report", "run"), ("Edit", "edit")]))


async def screen_edit(ctx, cid: int):
    await panel(ctx, cid, "<b>What needs changing?</b>",
                kb([("Project", "pick:p"), ("Style", "pick:s")],
                   [("Name", "rename"), ("Add links", "relink")],
                   [("Back", "back")]))


async def screen_projects(ctx, cid: int):
    api: ReportMaker = ctx.application.bot_data["api"]
    projects, current = await api.projects()
    projects = [p for p in projects if not p.get("archived")]
    sess(cid)["_projects"] = projects
    chosen = pref(cid, "project") or current.get("id")
    rows = [[(("• " if p["id"] == chosen else "") + pname(p), f"p:{i}")]
            for i, p in enumerate(projects[:20])]
    await panel(ctx, cid, "<b>Which project?</b>", kb(*rows))


async def screen_styles(ctx, cid: int):
    s = sess(cid)
    await resolve_into(ctx, cid)
    styles = s.get("_styles") or []
    if not styles:
        return await panel(ctx, cid,
                           f"<b>{html.escape(pname(s['project']))} has no style</b>\n"
                           f"<i>Add one at {APP_URL} → Styles, or pick another "
                           "project.</i>",
                           kb([("Choose another project", "pick:p")]))
    chosen = pref(cid, "style")
    rows = [[(("• " if st["slug"] == chosen else "") + st["label"][:58], f"s:{i}")]
            for i, st in enumerate(styles[:20])]
    await panel(ctx, cid, "<b>Which style?</b>", kb(*rows))


# --------------------------------------------------------------------------- #
# Resolving project + style
# --------------------------------------------------------------------------- #
async def resolve_into(ctx, cid: int):
    """Load this chat's project and style into the session.

    Falls back to whatever the dashboard has selected, and to the only style
    there is — never asking a question that has one answer.
    """
    api: ReportMaker = ctx.application.bot_data["api"]
    s = sess(cid)
    pid = pref(cid, "project")
    project = await api.project(pid) if pid else await api.current_project()
    styles = [x for x in (project.get("styles") or []) if not x.get("missing")]
    slug = pref(cid, "style")
    style = next((x for x in styles if x["slug"] == slug), None)
    if style is None and len(styles) == 1:
        style = styles[0]
    s["project"], s["style"], s["_styles"] = project, style, styles
    if style:
        set_pref(cid, project=project["id"], style=style["slug"])


async def reprice(ctx, cid: int) -> str:
    """Ask the app what would actually be captured. Returns '' or an error."""
    api: ReportMaker = ctx.application.bot_data["api"]
    s = sess(cid)
    if not s["style"]:
        return "no style"
    s["platform"] = s["style"].get("platform") or "combined"
    try:
        prev = await api.preview(text="\n".join(s["links"]), file=s["file"],
                                 platform=s["platform"])
    except ApiError as e:
        return str(e)
    s["count"] = prev.get("count") or 0
    s["dupes"] = prev.get("duplicate_count") or 0
    s["dropped"] = prev.get("dropped_count") or 0
    return ""


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text(
            "<b>This bot is private.</b>\n"
            f"Ask an administrator to add you. Your Telegram id is "
            f"<code>{update.effective_user.id}</code>.",
            parse_mode=ParseMode.HTML)
    cid = cid_of(update)
    first = cid not in S
    S[cid] = new_session()
    try:
        if first:
            await ctx.bot.send_message(
                cid,
                "<b>Report Maker</b>\n"
                "Send post links and get back a finished report.\n\n"
                "Pick the project and style below, then send the links — "
                "pasted, forwarded one at a time, or as a spreadsheet. "
                "You'll see a summary before anything runs."
                + ("\n\n<i>In a group, mention me to begin.</i>"
                   if update.effective_chat.type != ChatType.PRIVATE else ""),
                parse_mode=ParseMode.HTML)
        await screen_menu(ctx, cid, fresh=True)
    except ApiError as e:
        await ctx.bot.send_message(cid, f"{e}")


async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """The most recent report of this chat, with its download buttons back."""
    if not allowed(update):
        return
    cid = cid_of(update)
    job_id = LAST.get(cid)
    if not job_id:
        return await ctx.bot.send_message(cid, "No report from this chat yet.")
    api: ReportMaker = ctx.application.bot_data["api"]
    try:
        job = await api.status(job_id)
    except ApiError as e:
        return await ctx.bot.send_message(cid, f"{e}")
    batch = (RUNS.get(job_id) or {}).get("batch") or {"name": job.get("name") or "Report"}
    sess(cid)["panel"] = None                 # a fresh panel, not the old one
    await panel(ctx, cid, progress_text(job, batch),
                format_kb(job_id, job.get("artifacts") or [], set()))


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        cid = cid_of(update)
        cancel_settle(sess(cid))
        S[cid] = new_session()
        await ctx.bot.send_message(cid, "Discarded.")


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
async def _settle(cid: int, ctx):
    try:
        await asyncio.sleep(SETTLE_SECONDS)
    except asyncio.CancelledError:
        return
    s = sess(cid)
    if s["state"] == "collecting" and s["links"]:
        await screen_done(ctx, cid)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not addressed_to_us(update):
        return
    cid = cid_of(update)
    s = sess(cid)
    body = update.message.text or update.message.caption or ""
    if BOT_USERNAME:
        body = re.sub(rf"@{re.escape(BOT_USERNAME)}\b", " ", body, flags=re.I)

    if s["state"] == "naming":
        s["name"] = body.strip()[:80] or default_name()
        # Go through the same finish path as Skip, so the card is priced from
        # a real preview instead of whatever counts happened to be lying around.
        return await _finish_collecting(ctx, cid, skip_name=True)

    links = find_links(body)
    if not links:
        if s["state"] == "idle":
            await screen_menu(ctx, cid, fresh=True)
        return

    if s["state"] in ("idle", "confirm"):
        # Links arriving out of the blue start a batch; links arriving while a
        # card is on screen join it rather than opening a second one.
        if s["state"] == "idle":
            S[cid] = s = new_session()
        s["state"] = "collecting"
    if not s["by"]:
        s["by"] = who_of(update)

    fresh = [u for u in links if u not in s["seen"]]
    s["seen"].update(fresh)
    s["links"].extend(fresh)
    s["file"] = None
    cancel_settle(s)
    s["task"] = ctx.application.create_task(_settle(cid, ctx))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not addressed_to_us(update):
        return
    cid = cid_of(update)
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_INPUT_BYTES:
        return await ctx.bot.send_message(cid, "That file is over the 5 MB limit.")
    s = sess(cid)
    cancel_settle(s)
    tg_file = await doc.get_file()
    s["file"] = (doc.file_name, bytes(await tg_file.download_as_bytearray()),
                 doc.mime_type or "application/octet-stream")
    s["links"], s["seen"] = [], set()
    s["name"] = (update.message.caption or "").strip()[:80] or Path(doc.file_name).stem
    s["state"] = "collecting"
    s["by"] = s["by"] or who_of(update)
    await _finish_collecting(ctx, cid, skip_name=True)


def default_name() -> str:
    return time.strftime("Report %d-%m-%y %H:%M")


async def _finish_collecting(ctx, cid: int, skip_name: bool = False):
    s = sess(cid)
    cancel_settle(s)
    await resolve_into(ctx, cid)
    if not s["style"]:
        return await screen_styles(ctx, cid)
    if not skip_name and not s["name"]:
        return await screen_name(ctx, cid)
    s["name"] = s["name"] or default_name()
    await panel(ctx, cid, "<i>Checking the links…</i>")
    err = await reprice(ctx, cid)
    if err:
        return await panel(ctx, cid, f"<i>{html.escape(err)}</i>",
                           kb([("Edit", "edit")]))
    await screen_card(ctx, cid)


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    cid, data = cid_of(update), (q.data or "")
    s = sess(cid)

    try:
        if data.startswith("get:"):
            _, job_id, kind = data.split(":", 2)
            return await send_artifact(ctx, cid, job_id, kind)

        if data.startswith("stop:"):
            job_id = data.split(":", 1)[1]
            await ctx.application.bot_data["api"].cancel(job_id)
            return
        if data == "restart":
            S[cid] = new_session()
            return await screen_menu(ctx, cid, fresh=True)

        if data == "go":
            return await screen_ask_links(ctx, cid)
        if data == "pick:p":
            return await screen_projects(ctx, cid)
        if data == "pick:s":
            return await screen_styles(ctx, cid)

        if data.startswith("p:"):
            chosen = (s.get("_projects") or [])[int(data[2:])]
            if pref(cid, "project") != chosen["id"]:
                set_pref(cid, project=chosen["id"], style=None)
            await resolve_into(ctx, cid)
            if not s["style"]:
                return await screen_styles(ctx, cid)
            return await _after_setting(ctx, cid)

        if data.startswith("s:"):
            set_pref(cid, style=(s.get("_styles") or [])[int(data[2:])]["slug"])
            await resolve_into(ctx, cid)
            return await _after_setting(ctx, cid)

        if data == "wait":
            return await panel(ctx, cid,
                               f"<b>{len(s['links'])} links</b>\n"
                               "<i>Still listening — send the rest.</i>")
        if data == "done":
            return await _finish_collecting(ctx, cid)
        if data == "skip":
            s["name"] = default_name()
            return await _finish_collecting(ctx, cid, skip_name=True)

        if data == "edit":
            return await screen_edit(ctx, cid)
        if data == "back":
            return await screen_card(ctx, cid)
        if data == "rename":
            return await screen_name(ctx, cid)
        if data == "relink":
            s["state"] = "collecting"
            return await panel(ctx, cid, "<b>Send the extra links.</b>",
                               kb([("Done", "done")]))

        if data == "run":
            s["ran_by"] = who_of(update)
            s["by"] = s["by"] or s["ran_by"]
            if not s["count"]:
                return await panel(ctx, cid,
                                   "<b>Nothing to capture</b>\n"
                                   "<i>No usable post links for this style.</i>",
                                   kb([("Edit", "edit")]))
            batch = dict(s)
            S[cid] = new_session()
            S[cid]["panel"] = batch["panel"]
            return ctx.application.create_task(run_job(ctx, cid, batch))
    except ApiError as e:
        return await panel(ctx, cid, f"<i>{html.escape(str(e))}</i>",
                           kb([("Back", "back")]))
    except Exception as e:
        log.exception("button %r failed", data)
        return await panel(ctx, cid, f"<i>{html.escape(str(e))}</i>")


async def _after_setting(ctx, cid: int):
    """Back to wherever the person was when they opened the picker."""
    s = sess(cid)
    if s["links"] or s["file"]:
        return await _finish_collecting(ctx, cid, skip_name=bool(s["name"]))
    return await screen_menu(ctx, cid)


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def bar(done: int, total: int, width: int = 16) -> str:
    """A drawn bar rather than blocks of emoji — it reads as a instrument."""
    if not total:
        return "░" * width
    f = max(0, min(width, round(width * done / total)))
    return "█" * f + "░" * (width - f)


def progress_text(job: dict, b: dict) -> str:
    st = job.get("status", "")
    done = job.get("done") or 0
    total = job.get("total") or job.get("link_count") or 0
    head = "<b>" + html.escape(b["name"]) + "</b>\n"
    if st == "queued":
        body = "<i>Queued — waiting for a capture slot.</i>"
    elif st == "done":
        skipped = job.get("skipped") or []
        kept = max(0, total - len(skipped))
        body = (f"<b>Complete</b>   {kept} post{'s' if kept != 1 else ''}"
                f" · {elapsed(job.get('elapsed') or 0)}")
        if skipped:
            # Silence here is how a missing Facebook post goes unnoticed until
            # someone opens the PDF and counts.
            lines = "\n".join(
                "· " + html.escape(_short_link(x.get("link") or "")) + " — "
                + html.escape(x.get("reason") or "not captured")
                for x in skipped[:6])
            more = (f"\n<i>…and {len(skipped) - 6} more</i>"
                    if len(skipped) > 6 else "")
            body += (f"\n\n<b>{len(skipped)} link"
                     f"{'s' if len(skipped) != 1 else ''} not included</b>\n"
                     f"<i>{lines}{more}</i>")
        body += "\n\n<i>Choose a format below.</i>"
    elif st == "cancelled":
        body = "<b>Cancelled</b>"
    elif st in ("failed", "interrupted"):
        body = "<b>Failed</b>\n<i>" + html.escape(job.get("error") or st) + "</i>"
    else:
        pct = round(100 * done / total) if total else 0
        body = (f"{bar(done, total)}   {pct}%\n"
                f"<i>{html.escape(job.get('phase') or 'Working')} · "
                f"{done} of {total}</i>")
    tail = ""
    if b.get("by"):
        tail = "\n\n<i>Requested by " + html.escape(b["by"])
        if b.get("ran_by") and b["ran_by"] != b["by"]:
            tail += ", run by " + html.escape(b["ran_by"])
        tail += "</i>"
    return head + body + tail


def _short_link(url: str) -> str:
    """Enough of a URL to recognise it, not enough to wrap over three lines."""
    u = re.sub(r"^https?://(www\.)?", "", url or "")
    return u if len(u) <= 46 else u[:43] + "…"


def format_kb(job_id: str, artifacts, sent: set):
    rows, r = [], []
    for k in artifacts:
        if k in sent:
            continue
        r.append(InlineKeyboardButton(FORMAT_LABEL.get(k, k.upper()),
                                      callback_data=f"get:{job_id}:{k}"))
        if len(r) == 3:
            rows.append(r); r = []
    if r:
        rows.append(r)
    return InlineKeyboardMarkup(rows) if rows else None


async def run_job(ctx, cid: int, b: dict):
    """Submit, poll, deliver. Runs detached, so it catches everything."""
    api: ReportMaker = ctx.application.bot_data["api"]
    name = b["name"]
    try:
        res = await api.submit(report_name=name, report_type=b["style"]["slug"],
                               platform=b["platform"], text="\n".join(b["links"]),
                               file=b["file"], project_id=b["project"]["id"])
        job_id = res.get("job_id")
        RUNS[job_id] = {"chat": cid, "sent": set(), "batch": b}
        LAST[cid] = job_id
        stop = kb([("Stop", f"stop:{job_id}")])
        job = {}
        while True:
            await asyncio.sleep(POLL_SECONDS)
            job = await api.status(job_id)
            await panel(ctx, cid, progress_text(job, b),
                        None if job.get("finished") else stop)
            if job.get("finished"):
                break

        if job.get("status") != "done":
            return await panel(ctx, cid, progress_text(job, b),
                               kb([("New report", "restart")]))
        # Nothing is pushed: the style builds every format it knows how to,
        # and the reader takes the one they actually want.
        await panel(ctx, cid, progress_text(job, b),
                    format_kb(job_id, job.get("artifacts") or [], set()))
    except ApiError as e:
        await panel(ctx, cid, f"<b>{html.escape(name)}</b>\n"
                              f"<b>Failed</b>\n<i>{html.escape(str(e))}</i>",
                    kb([("New report", "restart")]))
    except Exception as e:
        # Without this the task dies silently and the progress bar freezes
        # forever — which is exactly what it did before.
        log.exception("run failed")
        await panel(ctx, cid, f"<b>{html.escape(name)}</b>\n"
                              f"<b>Failed</b>\n<i>{html.escape(str(e))}</i>",
                    kb([("New report", "restart")]))


async def send_artifact(ctx, cid: int, job_id: str, kind: str):
    api: ReportMaker = ctx.application.bot_data["api"]
    run = RUNS.setdefault(job_id, {"chat": cid, "sent": set()})
    try:
        filename, blob = await api.download(job_id, kind)
    except ApiError as e:
        return await ctx.bot.send_message(cid, f"{e}")
    if len(blob) > MAX_SEND_BYTES:
        return await ctx.bot.send_message(
            cid, f"{FORMAT_LABEL.get(kind, kind)} is {len(blob) // (1024*1024)} MB "
                 f"— too big for Telegram. {APP_URL}/jobs/{job_id}")
    await ctx.bot.send_document(cid, document=InputFile(blob, filename=filename))
    run["sent"].add(kind)


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
async def _post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("i am @%s", BOT_USERNAME)
    api: ReportMaker = app.bot_data["api"]
    try:
        await api.login()
        log.info("signed in to %s as %s", api.base, api.username)
    except ApiError as e:
        # Never die here. `restart: unless-stopped` would bring us straight
        # back, and seconds of that trips Report Maker's login rate limiter —
        # the bot locks itself out of the app it is trying to use. The client
        # signs in lazily, so start anyway and let the error reach the chat.
        log.error("not signed in yet: %s", e)
    await app.bot.set_my_commands([
        ("start", "Build a report"),
        ("last", "The last report from this chat"),
        ("cancel", "Discard the one in progress"),
    ])


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    missing = [k for k, v in (("TG_BOT_TOKEN", BOT_TOKEN), ("APP_USER", APP_USER),
                              ("APP_PASSWORD", APP_PASSWORD)) if not v]
    if missing:
        raise SystemExit("Missing in tg/.env: " + ", ".join(missing))

    load_prefs()
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.bot_data["api"] = ReportMaker(APP_URL, APP_USER, APP_PASSWORD)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_text))

    log.info("polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

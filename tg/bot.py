#!/usr/bin/env python3
"""Report bot — Telegram front door for Report Maker.

Send it post links — one message with twenty, or twenty forwarded messages
with one each — and it captures them, builds the report and sends the PDF
back. Everything is chosen by tapping.

Design rules it follows:
  * never ask a question that has one answer — one project, or one style, and
    the picker is skipped entirely
  * remember the last choice per person, so the second report is one tap
  * the platform comes from the STYLE, never guessed from the links
  * one message per run: it becomes the counter, then the card, then the
    progress bar, then the delivery
  * links that arrive late are added to the batch, never dropped
  * the server's refusal text is what the user reads — no invented errors

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
    """Read tg/.env without adding a dependency. Real env wins."""
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
# How long the chat must stay quiet before a batch closes itself into a card.
# Generous on purpose: nothing is lost by waiting, and links that arrive after
# a card is already showing are merged into it anyway.
COLLECT_SECONDS = float(os.environ.get("TG_COLLECT_SECONDS", "30") or 30)

# Telegram refuses documents over 50 MB from a bot; stay under it.
MAX_SEND_BYTES = 45 * 1024 * 1024
MAX_INPUT_BYTES = 5 * 1024 * 1024

FORMAT_LABEL = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint",
                "xlsx": "Excel", "zip": "Screenshots", "csv": "Numbers CSV"}

PREFS_PATH = Path(os.environ.get("TG_DATA_DIR", str(HERE / "data"))) / "prefs.json"

# Filled in at startup from getMe. In a group the bot answers only when it is
# tagged — or when a batch it already started is still open.
BOT_USERNAME = ""

# Everything below is keyed by CHAT id, not by person. In a private chat those
# are the same number; in a group it means one batch and one project/style for
# the whole team, which is how a team chat actually gets used.
PREFS: dict = {}       # chat id -> {"project": id, "style": slug}  (persisted)
PENDING: dict = {}     # chat id -> the batch showing a confirm card
BUFFERS: dict = {}     # chat id -> links still arriving
CACHE: dict = {}       # chat id -> picker lists, so callback_data stays short
RUNS: dict = {}        # job id -> delivery info for the format buttons
# People type the report name as its own message and then start forwarding;
# without this it would be discarded as "no links in that message".
TITLES: dict = {}
TITLE_TTL = 900.0


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
    except Exception as e:                       # never kill a run over this
        log.warning("could not save prefs: %s", e)


def pref(cid: int, key: str, default=None):
    return (PREFS.get(str(cid)) or {}).get(key, default)


def set_pref(cid: int, **kw):
    PREFS.setdefault(str(cid), {}).update(kw)
    save_prefs()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def allowed(update: Update) -> bool:
    """Empty allowlist = open (fine while testing locally, set it on the VPS).

    Checked against the PERSON, while everything else is keyed by chat: in a
    group only listed colleagues can drive the bot, but the batch they build
    belongs to the group.
    """
    user = update.effective_user
    return not ALLOWED or (user and user.id in ALLOWED)


def chat_id_of(update: Update) -> int:
    return update.effective_chat.id


def addressed_to_us(update: Update) -> bool:
    """In a group, act only when tagged — or when we are mid-batch.

    Requiring @mention on every one of twenty forwards would be absurd, so a
    tag opens the batch and the open batch keeps it going. A reply to one of
    our own messages counts as a tag too.
    """
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    msg = update.effective_message
    body = (msg.text or msg.caption or "")
    if BOT_USERNAME and f"@{BOT_USERNAME}".lower() in body.lower():
        return True
    reply = msg.reply_to_message
    if reply and reply.from_user and reply.from_user.is_bot \
            and reply.from_user.username == BOT_USERNAME:
        return True
    return chat.id in BUFFERS or chat.id in PENDING


def bar(done: int, total: int, width: int = 12) -> str:
    if not total:
        return "▱" * width
    filled = max(0, min(width, round(width * done / total)))
    return "▰" * filled + "▱" * (width - filled)


def estimate(count: int) -> str:
    """Rough wall-clock, from the pipeline's own ~10-12 s per post."""
    return f"~{max(1, round(count * 11 / 60))} min"


def default_name() -> str:
    return time.strftime("Report %d-%m-%y %H:%M")


def project_title(p: dict) -> str:
    return f"{p.get('emoji') or '📁'} {p.get('name') or 'Unsorted'}".strip()


def usable_styles(project: dict) -> list:
    """Styles the project can actually print in — a slug deleted from the pool
    is kept by the server flagged `missing`, and must not be offered."""
    return [s for s in (project.get("styles") or []) if not s.get("missing")]


async def resolve(cid: int, api: ReportMaker) -> tuple:
    """(project, chosen style or None, usable styles) for this person.

    The project falls back to whatever the dashboard has selected until they
    pick one; the style to their last choice, or to the only one there is.
    """
    pid = pref(cid, "project")
    project = await api.project(pid) if pid else await api.current_project()
    styles = usable_styles(project)
    slug = pref(cid, "style")
    style = next((s for s in styles if s["slug"] == slug), None)
    if style is None and len(styles) == 1:
        style = styles[0]
    return project, style, styles


async def _show(target, text, keyboard=None):
    """Rewrite whatever message we are working in.

    A CallbackQuery edits the message its button sat on; a Message we sent
    earlier is edited in place — that is what keeps a whole run to a single
    message. Identical content is skipped: Telegram answers 400 "message is
    not modified" and it would be pure log noise.
    """
    sig = (text, str(keyboard))
    if getattr(target, "_last_shown", None) == sig:
        return target

    async def write(body, parse):
        if hasattr(target, "edit_message_text"):          # CallbackQuery
            return await target.edit_message_text(body, parse_mode=parse,
                                                  reply_markup=keyboard)
        if hasattr(target, "edit_text"):                  # Message we own
            return await target.edit_text(body, parse_mode=parse,
                                          reply_markup=keyboard)
        return await target.reply_text(body, parse_mode=parse,
                                       reply_markup=keyboard)

    try:
        out = await write(text, ParseMode.HTML)
        target._last_shown = sig
        return out
    except BadRequest as e:
        said = str(e).lower()
        if "not modified" in said:
            target._last_shown = sig                      # already says this
            return target
        if "parse" in said or "entit" in said:
            # Something in the text was not valid HTML. Say so in the log and
            # still show the user their message, unformatted, rather than
            # silently dropping it.
            log.warning("Telegram rejected the markup (%s); resending plain", e)
            try:
                out = await write(re.sub(r"<[^>]+>", "", text), None)
                target._last_shown = sig
                return out
            except Exception as e2:
                log.warning("plain resend failed too: %s", e2)
            return target
        log.warning("edit failed: %s", e)                  # visible, not hidden
        return target
    except Exception as e:
        log.warning("show failed: %s", e)
        return target


def chat_of(target) -> int:
    msg = getattr(target, "message", None) or target
    return getattr(msg, "chat_id", None) or msg.chat.id


# --------------------------------------------------------------------------- #
# Pickers
# --------------------------------------------------------------------------- #
def _no_styles(project: dict) -> str:
    """A project with nothing to print in is a dead end unless we offer a way
    out — hence the button that comes with this message."""
    return (f"<b>{html.escape(project_title(project))}</b> has no style yet.\n\n"
            f"Open {APP_URL} → Styles → <i>Pick from the style pool</i>, "
            "or choose a different project below.")


def _other_project_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📁 Choose another project", callback_data="pick:project")]])


async def show_projects(target, ctx, cid: int, note: str = ""):
    api: ReportMaker = ctx.application.bot_data["api"]
    projects, current = await api.projects()
    projects = [p for p in projects if not p.get("archived")]
    CACHE.setdefault(cid, {})["projects"] = projects
    chosen = pref(cid, "project") or current.get("id")
    rows = [[InlineKeyboardButton(
        ("● " if p["id"] == chosen else "") + project_title(p), callback_data=f"pj:{i}")]
        for i, p in enumerate(projects[:20])]
    await _show(target, (note + "\n\n" if note else "") + "<b>Which project?</b>",
                InlineKeyboardMarkup(rows))


async def show_styles(target, ctx, cid: int, note: str = ""):
    api: ReportMaker = ctx.application.bot_data["api"]
    project, _style, styles = await resolve(cid, api)
    CACHE.setdefault(cid, {})["styles"] = styles
    if not styles:
        return await _show(target, _no_styles(project), _other_project_kb())
    chosen = pref(cid, "style")
    rows = [[InlineKeyboardButton(
        ("● " if s["slug"] == chosen else "") + f"🎨 {s['label']}"[:60],
        callback_data=f"st:{i}")] for i, s in enumerate(styles[:20])]
    await _show(target, (note + "\n\n" if note else "")
                + f"<b>Style for {html.escape(project_title(project))}</b>",
                InlineKeyboardMarkup(rows))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text(
            f"This bot is private. Your Telegram id is "
            f"{update.effective_user.id} — ask an admin to add it.")
    api: ReportMaker = ctx.application.bot_data["api"]
    where = ""
    try:
        project, style, _ = await resolve(chat_id_of(update), api)
        where = (f"\n\nPrinting into <b>{html.escape(project_title(project))}</b>"
                 + (f" · <b>{html.escape(style['label'])}</b>" if style else ""))
    except ApiError:
        pass
    await update.message.reply_text(
        "<b>Report bot</b>\n\n"
        "Send me post links — all in one message, or forwarded one at a time. "
        "I'll count them as they arrive; tap <b>Run now</b> when you're done "
        "and the PDF comes back here.\n\n"
        "Send the report's name as its own message first and I'll use it."
        + ("\n\n<i>In a group, tag me to start — after that I keep collecting "
           "until the batch is run.</i>" if BOT_USERNAME and
           update.effective_chat.type != ChatType.PRIVATE else "")
        + where,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📁 Project", callback_data="pick:project"),
            InlineKeyboardButton("🎨 Style", callback_data="pick:style")]]))


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await show_projects(update.message, ctx, chat_id_of(update))


async def cmd_style(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await show_styles(update.message, ctx, chat_id_of(update))


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"id {u.id} · @{u.username or '—'}")


# --------------------------------------------------------------------------- #
# Collecting
# --------------------------------------------------------------------------- #
def collecting_text(buf: dict) -> str:
    n = len(buf["links"])
    head = f"<b>{html.escape(buf['name'])}</b>\n" if buf.get("name") else ""
    where = ""
    if buf.get("project_name"):
        where = ("\n📁 " + html.escape(buf["project_name"])
                 + ("   🎨 " + html.escape(buf["style_label"])
                    if buf.get("style_label") else "   🎨 <i>not chosen</i>"))
    return (head + f"📥  <b>{n}</b> link{'s' if n != 1 else ''} collected" + where
            + "\n<i>Still listening — send more, or tap Run now.</i>")


def collecting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Run now", callback_data="collect:run")],
        [InlineKeyboardButton("📁 Project", callback_data="pick:project"),
         InlineKeyboardButton("🎨 Style", callback_data="pick:style"),
         InlineKeyboardButton("✕", callback_data="collect:drop")]])


async def _close_after_quiet(cid: int, ctx):
    """Close the batch once the links stop arriving.

    Restarted on every new message, so the countdown only runs while the chat
    is actually quiet. Cancelled outright when someone taps Run now.
    """
    try:
        await asyncio.sleep(COLLECT_SECONDS)
    except asyncio.CancelledError:
        return
    await close_batch(cid, ctx)


async def close_batch(cid: int, ctx, run: bool = False) -> None:
    """Turn the collected links into a confirm card — and run it, if asked."""
    buf = BUFFERS.pop(cid, None)
    if not buf:
        return
    task = buf.get("task")
    # Never cancel the countdown from inside the countdown — cancelling the
    # running task raises CancelledError at the very next await and the card is
    # never drawn. Only a tap reaches here with a live timer.
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    if not buf["links"]:
        return await _show(buf["msg"], "Nothing collected.")

    PENDING[cid] = {"links": list(buf["links"]), "seen": set(buf["seen"]),
                    "file": None, "name": buf["name"] or default_name(),
                    "msg": buf["msg"]}
    await _show(buf["msg"], "Reading…")
    try:
        await build_card(buf["msg"], ctx, cid, run=run)
    except Exception as e:
        # This runs detached in a task, where an exception would otherwise
        # vanish and leave the user staring at "Reading…" forever.
        log.exception("closing the batch failed")
        await _show(buf["msg"], f"⚠️ {html.escape(str(e))}")


# --------------------------------------------------------------------------- #
# The confirm card
# --------------------------------------------------------------------------- #
def confirm_card(b: dict) -> str:
    lines = [f"<b>{html.escape(b['name'])}</b>",
             f"{b['count']} link(s) · {estimate(b['count'])}",
             f"📁 {html.escape(b['project_name'])}   🎨 {html.escape(b['style_label'])}"]
    if b.get("duplicate_count"):
        lines.append(f"· {b['duplicate_count']} duplicate(s) removed")
    if b.get("dropped_count"):
        lines.append(f"· {b['dropped_count']} row(s) skipped — not a "
                     f"{b['platform']} post link")
    lines.append("<i>Still listening — more links are added to this batch.</i>")
    return "\n".join(lines)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Run", callback_data="run")],
        [InlineKeyboardButton("📁 Project", callback_data="pick:project"),
         InlineKeyboardButton("🎨 Style", callback_data="pick:style"),
         InlineKeyboardButton("✕", callback_data="drop")]])


async def build_card(target, ctx, cid: int, run: bool = False):
    """Resolve project + style, preview the pending input, draw the card.

    Called when a batch closes and again whenever the project, the style or
    the link list changes, so the counts always match what will actually run.
    """
    api: ReportMaker = ctx.application.bot_data["api"]
    batch = PENDING.get(cid)
    if not batch:
        return await _show(target, "That request expired — send the links again.")

    project, style, styles = await resolve(cid, api)
    if not styles:
        return await _show(target, _no_styles(project), _other_project_kb())
    if style is None:                       # several, and no choice made yet
        return await show_styles(target, ctx, cid,
                                 "Which style should this print in?")
    set_pref(cid, project=project["id"], style=style["slug"])

    # The style decides the platform — that pairing is what the server checks.
    platform = style.get("platform") or "combined"
    try:
        prev = await api.preview(text="\n".join(batch.get("links") or []),
                                 file=batch.get("file"), platform=platform)
    except ApiError as e:
        return await _show(target, f"⚠️ {html.escape(str(e))}", confirm_keyboard())

    batch.update(project_id=project["id"], project_name=project_title(project),
                 style=style["slug"], style_label=style["label"], platform=platform,
                 count=prev.get("count") or 0,
                 duplicate_count=prev.get("duplicate_count") or 0,
                 dropped_count=prev.get("dropped_count") or 0)
    if run and batch["count"]:
        PENDING.pop(cid, None)
        await _show(target, f"<b>{html.escape(batch['name'])}</b>\n⏳  Starting…")
        return ctx.application.create_task(_run(target, chat_of(target), ctx, batch))
    await _show(target, confirm_card(batch), confirm_keyboard())


# --------------------------------------------------------------------------- #
# Incoming messages
# --------------------------------------------------------------------------- #
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not addressed_to_us(update):
        return
    cid = chat_id_of(update)
    body = update.message.text or update.message.caption or ""
    if BOT_USERNAME:                       # the tag itself is not part of the text
        body = re.sub(rf"@{re.escape(BOT_USERNAME)}\b", " ", body, flags=re.I)
    links = find_links(body)
    stripped = body.strip()
    looks_like_a_title = bool(stripped) and len(stripped) <= 80 and "\n" not in stripped

    if not links:
        buf = BUFFERS.get(cid)
        if buf is not None:
            # A line with no link, mid-collection, is the report name if we do
            # not have one yet — otherwise it is just chatter.
            if looks_like_a_title and not buf["name"]:
                buf["name"] = stripped
                await _show(buf["msg"], collecting_text(buf), collecting_keyboard())
            return
        if looks_like_a_title:
            TITLES[cid] = (stripped, time.monotonic())
            return await update.message.reply_text(
                f"📝  Noted — the report will be called <b>{html.escape(stripped)}</b>.\n"
                "Send the links now, one message or twenty.",
                parse_mode=ParseMode.HTML)
        return await update.message.reply_text(
            "No links in that message. Paste post links, or upload a "
            ".xlsx / .csv / .txt file.")

    # Late arrivals join the batch that is already on screen rather than
    # starting a second one — the whole point of a quiet timer is that being
    # wrong about when someone finished costs nothing.
    batch = PENDING.get(cid)
    if batch is not None and batch.get("links") is not None:
        fresh = [u for u in links if u not in batch["seen"]]
        if fresh:
            batch["seen"].update(fresh)
            batch["links"].extend(fresh)
            await build_card(batch.get("msg") or update.message, ctx, cid)
        return

    buf = BUFFERS.get(cid)
    if buf is None:
        # "July report https://x.com/… https://x.com/…" — the words are the
        # name, the links are the batch. Drop any word the parser recognises
        # as a link rather than testing where the line starts.
        first = stripped.splitlines()[0].strip()
        first = " ".join(w for w in first.split() if not find_links(w))
        name = first.strip(" -–—:·|,.")
        if not name:                                  # …or one sent just before
            title, when = TITLES.pop(cid, ("", 0.0))
            if title and time.monotonic() - when < TITLE_TTL:
                name = title
        msg = await update.message.reply_text("📥  collecting…",
                                              reply_markup=collecting_keyboard())
        buf = BUFFERS[cid] = {"links": [], "seen": set(), "name": name[:80],
                              "msg": msg, "task": None, "last_edit": 0.0,
                              "last_n": 0, "project_name": "", "style_label": ""}
        # Shown on the counter so Run now is never a leap of faith. One call
        # per batch, not per message.
        try:
            project, style, _ = await resolve(cid, ctx.application.bot_data["api"])
            buf["project_name"] = project_title(project)
            buf["style_label"] = style["label"] if style else ""
        except ApiError:
            pass

    for url in links:                                   # dedupe across messages
        if url not in buf["seen"]:
            buf["seen"].add(url)
            buf["links"].append(url)

    if buf["task"] and not buf["task"].done():
        buf["task"].cancel()
    buf["task"] = ctx.application.create_task(_close_after_quiet(cid, ctx))

    # Twenty forwards land within a second or two. Editing on every one would
    # hit Telegram's per-chat edit limit; editing on a timer alone would leave
    # the counter reading "1" through the whole burst, which looks like the bot
    # missed them. So: refresh on a 1.5 s tick OR every five new links.
    now, n = time.monotonic(), len(buf["links"])
    if now - buf["last_edit"] > 1.5 or n - buf["last_n"] >= 5:
        buf["last_edit"], buf["last_n"] = now, n
        await _show(buf["msg"], collecting_text(buf), collecting_keyboard())


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not addressed_to_us(update):
        return
    cid = chat_id_of(update)
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_INPUT_BYTES:
        return await update.message.reply_text("That file is over the 5 MB limit.")
    buf = BUFFERS.pop(cid, None)                # a file supersedes a paste
    if buf and buf.get("task") and not buf["task"].done():
        buf["task"].cancel()
    tg_file = await doc.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    name = (update.message.caption or "").strip() or Path(doc.file_name).stem
    msg = await update.message.reply_text("Reading…")
    PENDING[cid] = {"links": None, "seen": set(), "name": name[:80] or default_name(),
                    "file": (doc.file_name, raw,
                             doc.mime_type or "application/octet-stream"),
                    "msg": msg}
    await build_card(msg, ctx, cid)


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data, cid = q.data or "", chat_id_of(update)

    if data.startswith("get:"):
        _, job_id, kind = data.split(":", 2)
        return await _send_artifact(ctx, chat_of(q), job_id, kind)

    if data == "collect:run":
        return await close_batch(cid, ctx, run=True)
    if data == "collect:drop":
        buf = BUFFERS.pop(cid, None)
        if buf and buf.get("task") and not buf["task"].done():
            buf["task"].cancel()
        return await _show(q, "Cancelled.")

    if data == "pick:project":
        return await show_projects(q, ctx, cid)
    if data == "pick:style":
        return await show_styles(q, ctx, cid)

    if data.startswith("pj:"):
        projects = (CACHE.get(cid) or {}).get("projects") or []
        i = int(data.split(":", 1)[1])
        if i >= len(projects):
            return await show_projects(q, ctx, cid, "That list went stale.")
        chosen = projects[i]
        # A different project means a different style list — clear the old pick
        # rather than carrying a slug this project may not even have.
        if pref(cid, "project") != chosen["id"]:
            set_pref(cid, project=chosen["id"], style=None)
        return await _after_pick(q, ctx, cid,
                                 f"📁 {html.escape(project_title(chosen))}")

    if data.startswith("st:"):
        styles = (CACHE.get(cid) or {}).get("styles") or []
        i = int(data.split(":", 1)[1])
        if i >= len(styles):
            return await show_styles(q, ctx, cid, "That list went stale.")
        set_pref(cid, style=styles[i]["slug"])
        return await _after_pick(q, ctx, cid,
                                 f"🎨 <b>{html.escape(styles[i]['label'])}</b>")

    batch = PENDING.get(cid)
    if not batch:
        return await _show(q, "That request expired — send the links again.")
    if data == "drop":
        PENDING.pop(cid, None)
        return await _show(q, "Cancelled.")
    if data == "run":
        if not batch.get("count"):
            return await _show(q, "Nothing to capture — send the links again.")
        PENDING.pop(cid, None)
        await _show(q, f"<b>{html.escape(batch['name'])}</b>\n⏳  Starting…")
        ctx.application.create_task(_run(q, chat_of(q), ctx, batch))


async def _after_pick(q, ctx, cid: int, note: str):
    """Go back to whatever the person was doing when they opened the picker."""
    if cid in BUFFERS:
        buf = BUFFERS[cid]
        api: ReportMaker = ctx.application.bot_data["api"]
        try:
            project, style, _ = await resolve(cid, api)
            buf["project_name"] = project_title(project)
            buf["style_label"] = style["label"] if style else ""
        except ApiError:
            pass
        return await _show(q, collecting_text(buf), collecting_keyboard())
    if PENDING.get(cid):
        return await build_card(q, ctx, cid)
    if note.startswith("📁"):
        return await show_styles(q, ctx, cid, note)
    return await _show(q, note + "\n\nSend me links whenever you're ready.")


# --------------------------------------------------------------------------- #
# The run: submit, poll into one edited message, deliver
# --------------------------------------------------------------------------- #
def progress_text(job: dict, name: str) -> str:
    status = job.get("status", "")
    done = job.get("done") or 0
    total = job.get("total") or job.get("link_count") or 0
    phase = job.get("phase") or status.title()
    if status == "queued":
        body = "⏳  Queued — waiting for a capture slot"
    elif status == "done":
        body = "✅  Done"
    elif status == "cancelled":
        body = "🚫  Cancelled"
    elif status in ("failed", "interrupted"):
        body = f"⚠️  {html.escape(job.get('error') or status)}"
    else:
        body = f"{bar(done, total)}  {done}/{total}\n{html.escape(phase)}"
    return f"<b>{html.escape(name)}</b>\n{body}"


def format_keyboard(job_id: str, artifacts, sent: set):
    rows, row = [], []
    for kind in artifacts:
        if kind in sent:
            continue
        row.append(InlineKeyboardButton(FORMAT_LABEL.get(kind, kind.upper()),
                                        callback_data=f"get:{job_id}:{kind}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


async def _run(target, chat_id: int, ctx, batch: dict):
    api: ReportMaker = ctx.application.bot_data["api"]
    name = batch["name"]
    try:
        res = await api.submit(report_name=name, report_type=batch["style"],
                               platform=batch["platform"],
                               text="\n".join(batch.get("links") or []),
                               file=batch.get("file"),
                               project_id=batch["project_id"], outputs=["pdf"])
    except ApiError as e:
        return await _show(target, f"<b>{html.escape(name)}</b>\n⚠️ {html.escape(str(e))}")

    job_id = res.get("job_id")
    RUNS[job_id] = {"name": name, "chat": chat_id, "sent": set()}
    job = {}
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            job = await api.status(job_id)
        except ApiError as e:
            return await _show(target,
                               f"<b>{html.escape(name)}</b>\n⚠️ {html.escape(str(e))}")
        await _show(target, progress_text(job, name))
        if job.get("finished"):
            break

    if job.get("status") != "done":
        return
    artifacts = job.get("artifacts") or []
    if "pdf" in artifacts:
        await _send_artifact(ctx, chat_id, job_id, "pdf")
    kb = format_keyboard(job_id, [k for k in artifacts if k != "pdf"],
                         RUNS[job_id]["sent"])
    tail = "\nAnother format? These build on the screenshots already taken." if kb else ""
    await _show(target, progress_text(job, name) + tail, kb)


async def _send_artifact(ctx, chat_id: int, job_id: str, kind: str):
    api: ReportMaker = ctx.application.bot_data["api"]
    run = RUNS.setdefault(job_id, {"chat": chat_id, "sent": set()})
    try:
        filename, blob = await api.download(job_id, kind)
    except ApiError as e:
        return await ctx.bot.send_message(chat_id, f"⚠️ {e}")
    if len(blob) > MAX_SEND_BYTES:
        return await ctx.bot.send_message(
            chat_id, f"The {FORMAT_LABEL.get(kind, kind)} is "
            f"{len(blob) // (1024 * 1024)} MB — over Telegram's 50 MB limit. "
            f"Download it from {APP_URL}/jobs/{job_id}")
    await ctx.bot.send_document(chat_id, document=InputFile(blob, filename=filename))
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
        # back, and a few seconds of that trips Report Maker's own login rate
        # limiter — the bot locks itself out of the app it is trying to use.
        # The client signs in lazily on first use, so we start anyway and the
        # real reason reaches whoever messages the bot.
        log.error("could not sign in to Report Maker yet: %s", e)
        log.error("the bot is running; fix the credentials and it will "
                  "connect on the next message — no restart needed.")
    await app.bot.set_my_commands([
        ("start", "How this works, and what it's set to"),
        ("project", "Choose the project runs go into"),
        ("style", "Choose the style reports print in"),
        ("whoami", "Your Telegram id"),
    ])


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per poll
    missing = [k for k, v in (("TG_BOT_TOKEN", BOT_TOKEN), ("APP_USER", APP_USER),
                              ("APP_PASSWORD", APP_PASSWORD)) if not v]
    if missing:
        raise SystemExit("Missing in tg/.env: " + ", ".join(missing))

    load_prefs()
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.bot_data["api"] = ReportMaker(APP_URL, APP_USER, APP_PASSWORD)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("style", cmd_style))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_text))

    log.info("polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

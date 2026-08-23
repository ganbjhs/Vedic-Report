"""
Read message history from the currently open chat.

Each message is returned as a dict:
  {id, sender, phone, time (datetime|None), text, links[list], outgoing(bool)}
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from .session import WASession

# WhatsApp puts "[10:32, 15/08/2026] Sender Name: " into data-pre-plain-text
_PRE = re.compile(r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>.*?):\s*$")

_JS_EXTRACT = r"""
() => {
  // WhatsApp Web 2025+: messages are #main [role="row"] with a [data-pre-plain-text]
  // bubble ("[10:32 AM, 8/18/2026] Sender: ") and data-testid="conv-msg-<id>".
  // Older builds: div.message-in / div.message-out. Both handled.
  const main = document.querySelector('#main');
  if (!main) return [];
  // innerText doubles line breaks and drops emoji (they are <img alt>), so walk the nodes
  const textOf = (el) => { let s = ''; const walk = (n) => { for (const c of n.childNodes) {
      if (c.nodeType === 3) s += c.nodeValue;
      else if (c.nodeType === 1) { if (c.tagName === 'IMG') s += c.getAttribute('alt') || '';
                                   else if (c.tagName === 'BR') s += '\n'; else walk(c); } } };
    walk(el); return s; };
  let rows = Array.from(main.querySelectorAll('[role="row"]'));
  if (!rows.length) rows = Array.from(main.querySelectorAll('div.message-in, div.message-out'));
  const out = [];
  for (const r of rows) {
    const pre = r.querySelector('[data-pre-plain-text]');
    const conv = r.querySelector('[data-testid^="conv-msg-"]') || r.closest('[data-testid^="conv-msg-"]');
    const idHost = r.querySelector('[data-id]') || r.closest('[data-id]');
    const id = conv ? conv.getAttribute('data-testid').slice(9) : (idHost ? idHost.getAttribute('data-id') : null);
    if (!pre && !conv) continue;                       // system rows ("Today", "You added…")
    const preText = pre ? pre.getAttribute('data-pre-plain-text') : '';
    let text = '';
    if (pre) { const c = pre.querySelector('.selectable-text'); text = textOf(c || pre); }
    if (!text) { const c = r.querySelector('.selectable-text, .copyable-text'); text = c ? textOf(c) : ''; }
    const links = Array.from(r.querySelectorAll('a[href]')).map(a => a.href).filter(h => /^https?:/.test(h));
    let outgoing = !!r.querySelector('.message-out') || r.classList.contains('message-out');
    if (!outgoing && !r.querySelector('.message-in') && !r.classList.contains('message-in')) {
      const labels = Array.from(r.querySelectorAll('[aria-label]')).map(e => (e.getAttribute('aria-label') || '').trim().toLowerCase());
      if (labels.some(l => ['delivered', 'read', 'sent', 'pending', 'you:'].includes(l))) outgoing = true;
      else {
        const box = r.querySelector('[data-testid="msg-container"]') || pre;
        if (box) { const rb = r.getBoundingClientRect(), bb = box.getBoundingClientRect();
                   outgoing = (rb.right - bb.right) < (bb.left - rb.left); }
      }
    }
    out.push({id, pre: preText, nameAria: '', text, links, outgoing});
  }
  return out;
}
"""

_JS_EXPAND = r"""
() => {
  // WhatsApp truncates long messages and shows "Read more"; VERY long messages
  // expand in ~3000-char chunks, so this must be called repeatedly until it returns 0.
  let n = 0;
  for (const el of document.querySelectorAll('#main [role="button"], #main span, #main div')) {
    const t = (el.textContent || '').trim();
    if ((t === 'Read more' || t === 'Show more') && el.children.length === 0) { el.click(); n++; }
  }
  return n;
}
"""

_JS_SCROLLER = r"""
() => {
  const first = document.querySelector('#main div.message-in, #main div.message-out');
  let el = first;
  while (el && el !== document.body) {
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
      return true;
    }
    el = el.parentElement;
  }
  return false;
}
"""

_JS_SCROLL_TOP = r"""
() => {
  const first = document.querySelector('#main [role="row"], #main div.message-in, #main div.message-out');
  let el = first;
  while (el && el !== document.body) {
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
      el.scrollTop = 0; return el.scrollHeight;
    }
    el = el.parentElement;
  }
  return -1;
}
"""

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+?(?=[.,;:!?]*(?:\s|$|[<>\"')\]]))", re.I)


def _parse_time(s: str) -> datetime | None:
    s = s.strip()
    for fmt in ("%H:%M, %d/%m/%Y", "%I:%M %p, %d/%m/%Y", "%H:%M, %m/%d/%Y", "%I:%M %p, %m/%d/%Y",
                "%H:%M, %d.%m.%Y", "%H:%M, %Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalise(raw: dict) -> dict:
    sender, ts = "", None
    m = _PRE.match(raw.get("pre") or "")
    if m:
        sender = m.group("sender").strip()
        ts = _parse_time(m.group("time"))
    if not sender and raw.get("nameAria"):
        sender = raw["nameAria"].rstrip(": ").strip()
    if raw.get("outgoing") and not sender:
        sender = "me"
    text = raw.get("text") or ""
    links = list(dict.fromkeys(list(raw.get("links") or []) + _URL_RE.findall(text)))
    phone = sender if re.fullmatch(r"\+?[\d\s\-()]{7,}", sender or "") else ""
    # Group message ids look like  false_1203...@g.us_3EB0..._919876543210@c.us
    # -> the trailing part is the participant's number (not present for @lid ids).
    mid = raw.get("id") or ""
    pm = re.search(r"_(\d{7,})@c\.us$", mid)
    if pm and not phone:
        phone = "+" + pm.group(1)
    return {"id": raw.get("id"), "sender": sender, "phone": phone, "time": ts,
            "text": text, "links": links, "outgoing": bool(raw.get("outgoing"))}


def read_visible(s: WASession, expand: bool = True) -> list[dict]:
    """Messages currently rendered in the open chat (no scrolling)."""
    if expand:
        try:
            for _ in range(25):                     # keep clicking until nothing is left to expand
                if not s.page.evaluate(_JS_EXPAND):
                    break
                time.sleep(0.45)
        except Exception:  # noqa: BLE001
            pass
    return [_normalise(r) for r in s.page.evaluate(_JS_EXTRACT)]


def read_history(s: WASession, max_messages: int = 300, since: datetime | None = None,
                 days: int | None = None, max_scrolls: int = 60, pause: float = 1.2) -> list[dict]:
    """
    Scroll upwards in the open chat until we have `max_messages`, or the oldest
    loaded message is older than `since`/`days`, or the chat stops growing.
    """
    if days and not since:
        since = datetime.now() - timedelta(days=days)
    seen_heights, msgs = [], []
    for i in range(max_scrolls):
        msgs = read_visible(s)
        oldest = next((m["time"] for m in msgs if m["time"]), None)
        if len(msgs) >= max_messages:
            break
        if since and oldest and oldest < since:
            break
        h = s.page.evaluate(_JS_SCROLL_TOP)
        time.sleep(pause)
        seen_heights.append(h)
        if len(seen_heights) >= 3 and len(set(seen_heights[-3:])) == 1:
            break  # nothing new loaded three times in a row
    if since:
        msgs = [m for m in msgs if not m["time"] or m["time"] >= since]
    return msgs[-max_messages:]

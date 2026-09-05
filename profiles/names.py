"""The account's DISPLAY NAME off the page that was just captured — "Yogi
Adityanath", not "@myogiadityanath".

WHY. The deck's "Handle Name" slot (and every caption) prints `account_name`.
When the sheet has no handle column, that is whatever the capture read — and
the X capture reads the @handle (`x_capture._read_handle`, frozen), the
Instagram capture the username. The Facebook engine reads the Page's NAME. So a
combined report said "Banaras Live" on one page and "@myogiadityanath" on the
next, and the team asked for the name as it is shown, never the @username.

HOW. Nothing under `src/` changes (rule 1). After a successful X or Instagram
capture the page is still open on the post, and its `<title>` already carries
the name the way the platform prints it — "Yogi Adityanath on X: "…"", "Nalini's
Kitchen on Instagram: "…"". That is read first. If the title is not in that
shape (a wall, an odd redirect), X's header block for the focused tweet is read
directly — the `User-Name` node whose text carries the @handle we already know,
so on a reply URL the PARENT's name cannot be taken by mistake (RULEBOOK §6.1) —
and Instagram's `og:title` meta says the same thing the title does.

Emoji and other pictographs are stripped: the deck prints in Helvetica with a
Devanagari face swapped in per string (rule 14 via `tpl_builder._drawable`),
and neither has a glyph for a flag or a lotus, which would print as boxes.

Pure Playwright reads, no navigation, no screenshot. Never raises: '' when
nothing usable was found, and the caller falls back to the handle without
its '@'.
"""
import re
import unicodedata

_TITLE = {
    "x": re.compile(r"^\s*(.+?)\s+on X\b"),
    "instagram": re.compile(r"^\s*(.+?)\s+on Instagram\b"),
}

# X's header for the focused tweet: the User-Name block whose text carries the
# handle we already read; its first line is the display name.
_JS_X_HEADER = """(handle) => {
  const blocks = document.querySelectorAll('article[data-testid="tweet"] [data-testid="User-Name"]');
  for (const b of blocks) {
    const txt = (b.innerText || '');
    if (handle && !txt.includes(handle)) continue;
    const line = txt.split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
    if (line && !line.startsWith('@')) return line;
  }
  return '';
}"""

_JS_OG_TITLE = """() => {
  const m = document.querySelector('meta[property="og:title"]');
  return (m && m.getAttribute('content')) || '';
}"""


def clean(name: str) -> str:
    """Whitespace-normalised, pictographs removed, never just an @handle."""
    out = []
    for ch in name or "":
        cat = unicodedata.category(ch)
        # So = other symbol (emoji, flags), Sk = modifier symbols, Cf = format
        # (zero-width joiner, variation selectors), Cs/Co = surrogates/private.
        if cat in ("So", "Sk", "Cf", "Cs", "Co") or ord(ch) >= 0x1F000:
            continue
        if ord(ch) == 0xFE0F:                 # emoji variation selector
            continue
        out.append(ch)
    s = re.sub(r"\s+", " ", "".join(out)).strip(" \t\r\n·•|-—")
    if not s or s.startswith("@"):
        return ""
    return s


def display_name(page, platform: str, handle: str = "") -> str:
    """The display name of the account behind the post `page` shows, or ''."""
    if platform not in _TITLE:
        return ""
    name = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    m = _TITLE[platform].match(title)
    if m:
        name = clean(m.group(1))
    if not name and platform == "x":
        try:
            name = clean(page.evaluate(_JS_X_HEADER, (handle or "").strip()) or "")
        except Exception:
            name = ""
    if not name and platform == "instagram":
        try:
            og = page.evaluate(_JS_OG_TITLE) or ""
        except Exception:
            og = ""
        m = _TITLE[platform].match(og)
        if m:
            name = clean(m.group(1))
    return name


def account_name(display: str, handle: str) -> str:
    """What the report prints when the sheet typed nothing: the display name,
    else the handle WITHOUT its '@' — never '@username' (the team's rule)."""
    d = clean(display or "")
    if d:
        return d
    h = (handle or "").strip()
    return h[1:] if h.startswith("@") else h

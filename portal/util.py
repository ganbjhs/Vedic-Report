"""Pure helpers shared by the portal and the publish step. Standard library only.

* `norm_url`      — one canonical form per post, so a link typed with a
                    tracking parameter and the same link without it are the
                    same row in `post_metrics`.
* `platform_of`   — x | facebook | instagram from a link (or a scraper's word).
* `to_int`        — "1.1K", "63,900", "2.1 lakh", "hidden", "—" → int or None.
                    NEVER 0 for a count the platform did not show.
* `today_in`      — today's date in the client's timezone.
* `day_str`/`parse_day` — YYYY-MM-DD both ways.
"""
import datetime as _dt
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # Python < 3.9 — not expected
    ZoneInfo = None

PLATFORMS = ("facebook", "instagram", "x")

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
             "igsh", "igshid", "igsi", "s", "t", "ref", "ref_src", "fbclid", "mibextid",
             "rdid", "share_url", "__cft__[0]", "__tn__", "sfnsn", "wtsid"}


def norm_url(url: str) -> str:
    """Canonical form of a post link. Host lowercased, `www.`/`m.`/`mobile.`
    dropped, `twitter.com` → `x.com`, tracking parameters removed, trailing
    slash removed, fragment removed. Never raises; an empty string stays empty."""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    try:
        parts = urlsplit(u)
    except ValueError:
        return u.lower()
    host = (parts.hostname or "").lower()
    for pre in ("www.", "m.", "mobile.", "mbasic."):
        if host.startswith(pre):
            host = host[len(pre):]
    if host == "twitter.com":
        host = "x.com"
    if host == "fb.com":
        host = "facebook.com"
    path = re.sub(r"/+$", "", parts.path or "")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in _TRACKING and not k.startswith("utm_")]
    # facebook keeps meaning in a few params (story_fbid, id, v, fbid); everything else goes
    if host.endswith("facebook.com"):
        keep = {"story_fbid", "id", "v", "fbid", "set"}
        query = [(k, v) for k, v in query if k in keep]
    else:
        query = []
    return urlunsplit(("https", host, path, urlencode(query), ""))


def platform_of(url: str = "", hint: str = "") -> str:
    s = f"{hint or ''} {url or ''}".lower()
    if "instagram" in s or "instagr.am" in s or re.search(r"\big\b", s):
        return "instagram"
    if "facebook" in s or "fb.watch" in s or "fb.com" in s or re.search(r"\bfb\b", s):
        return "facebook"
    return "x"


_NUM = re.compile(r"^\s*([\d,]*\.?\d+)\s*(k|m|b|lakh|lac|l|cr|crore)?\s*$", re.I)
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "lakh": 1e5, "lac": 1e5, "l": 1e5, "cr": 1e7, "crore": 1e7}
_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")


def to_int(value):
    """Parse a count the way the pictures and sheets print it. Returns None for
    anything that is not a number — '—', 'hidden', '', None — because a blank
    is the honest cell and a 0 would be a lie."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    s = str(value).strip().translate(_DEVANAGARI).replace(" ", " ")
    if not s:
        return None
    s = re.sub(r"(views?|likes?|comments?|shares?|reposts?|replies|reply|plays?)$", "", s, flags=re.I).strip()
    m = _NUM.match(s)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(round(n * _MULT.get((m.group(2) or "").lower(), 1)))


def day_str(d) -> str:
    if isinstance(d, str):
        return d[:10]
    return d.strftime("%Y-%m-%d")


def parse_day(s: str):
    """'2026-09-04' / '4/9/26' / '04-09-2026' → date, else None."""
    if not s:
        return None
    if isinstance(s, (_dt.date, _dt.datetime)):
        return s if isinstance(s, _dt.date) and not isinstance(s, _dt.datetime) else s.date()
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
        if y < 100:
            y += 2000
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def parse_when(value):
    """ISO string / epoch seconds / epoch ms → aware datetime (UTC if naive), else None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 2e10:
                v /= 1000.0
            return _dt.datetime.fromtimestamp(v, tz=_dt.timezone.utc)
        s = str(value).strip()
        if re.match(r"^\d{10,13}$", s):
            return parse_when(int(s))
        s = s.replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except (ValueError, OverflowError, OSError):
        return None


def today_in(tz: str = "Asia/Kolkata") -> _dt.date:
    if ZoneInfo is not None:
        try:
            return _dt.datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    return _dt.date.today()


def add_days(d: _dt.date, n: int) -> _dt.date:
    return d + _dt.timedelta(days=n)

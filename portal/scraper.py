"""The scraper adapter — fetch the team's own scraper and turn its JSON into
portal posts. Standard library only (urllib), so the internal app can run the
sync without the portal's dependencies.

Nothing here decides what the client SEES; that is `post_metrics` +
`visible_from`. This module only knows how to ask the scraper and how to read
its answer. Field names are matched loosely through FIELD_MAP — edit that
once against a real response and every caller follows.

    posts = fetch(source, api_key, day_from, day_to)        # list[dict] normalised
    rows  = normalize_many(json_body)                        # same, from a body you already have
"""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import util

# The names each portal field is looked for under, in order. Dotted paths dig
# into nested objects; `media.0.type` = first element of a `media` list.
FIELD_MAP = {
    "platform":     ["platform", "network", "source", "site"],
    "name":         ["name", "display_name", "author_display_name", "full_name", "author_name", "page_name",
                     "user.full_name", "user.name", "author.name", "owner.name"],
    "handle":       ["handle", "username", "author_username", "screen_name", "user.username", "author.username",
                     "owner.username", "account"],
    "avatar":       ["avatar", "avatar_url", "author_avatar", "profile_pic_url", "profile_image_url",
                     "user.profile_pic_url", "user.avatar", "author.avatar", "owner.profile_pic_url"],
    "text":         ["text", "caption", "content", "message", "full_text", "body"],
    "posted_at":    ["posted_at", "created_at", "timestamp", "taken_at", "date", "published_at"],
    "collected_at": ["collected_at", "scraped_at", "fetched_at", "crawled_at"],
    "url":          ["url", "permalink", "link", "post_url", "href"],
    "likes":        ["likes", "like_count", "favorite_count", "reactions", "reaction_count", "likes_count"],
    "comments":     ["comments", "comment_count", "reply_count", "replies", "comments_count"],
    "shares":       ["shares", "share_count", "repost_count", "retweet_count", "reposts", "shares_count"],
    "views":        ["views", "view_count", "play_count", "video_view_count", "views_count", "impressions"],
    "reach":        ["reach", "reach_count"],
    "category":     ["category", "group", "classification", "section", "label", "bucket"],
    # --- the Collector's own fields. Nothing here existed before v3.4 ---------
    "post_id":      ["tweet_id", "post_id", "id_str", "media_pk", "shortcode"],
    "day":          ["day", "sheet_date"],
    "status":       ["status"],
    "status_note":  ["status_note", "note"],
    "quotes":       ["quote_count", "quotes"],
    "bookmarks":    ["bookmark_count", "bookmarks"],
    "followers":    ["author_followers", "follower_count", "followers"],
    "lang":         ["lang", "language"],
    "refreshed_ms": ["last_refresh_ms", "refreshed_ms"],
    "refresh_count": ["refresh_count", "refreshes"],
    "media_type":   ["media_type", "type", "media.0.type", "media.0.media_type", "product_type"],
    "thumb":        ["thumbnail_url", "thumbnail", "thumb", "media.0.thumbnail_url", "media.0.url",
                     "image_url", "display_url", "preview_url"],
}

_TIMEOUT = 20
_MAX_BYTES = 25 * 1024 * 1024          # one page
_MAX_TOTAL_BYTES = 60 * 1024 * 1024    # every page of one walk
_MAX_PAGES = 50
_MAX_ROWS = 25_000
_DEFAULT_LIMIT = 500


class ScraperError(Exception):
    pass


def _dig(obj, path: str):
    cur = obj
    for key in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
        if cur is None:
            return None
    return cur


def _pick(obj: dict, field: str):
    for path in FIELD_MAP[field]:
        v = _dig(obj, path)
        if v is not None and v != "":
            return v
    return None


def normalize(raw: dict) -> dict:
    """One scraper post → the portal's post shape. Unknown fields are ignored;
    a missing count is None, never 0."""
    url = str(_pick(raw, "url") or "").strip()
    # A row that STATES its platform is believed. `platform_of()` ends with
    # `return "x"`, so guessing from the URL files a YouTube link as X — and a
    # platform this portal does not model must be skipped, never guessed at.
    stated = str(_pick(raw, "platform") or "").strip().lower()
    if stated in util.PLATFORMS:
        plat = stated
    elif stated:
        plat = ""                       # stated, and not one of ours -> caller skips it
    else:
        plat = util.platform_of(url, "")
    handle = str(_pick(raw, "handle") or "").strip().lstrip("@")
    posted = util.parse_when(_pick(raw, "posted_at"))
    collected = util.parse_when(_pick(raw, "collected_at"))
    refreshed_ms = util.to_int(_pick(raw, "refreshed_ms"))
    if collected is None and refreshed_ms:
        collected = util.parse_when(refreshed_ms)
    given_day = util.parse_day(str(_pick(raw, "day") or ""))
    status = str(_pick(raw, "status") or "").strip().lower()
    if status not in ("ok", "pending", "unavailable", "removed"):
        status = "ok" if not status else status
    mt = str(_pick(raw, "media_type") or "").lower()
    if any(w in mt for w in ("video", "reel", "clip", "igtv")):
        media_type = "video"
    elif any(w in mt for w in ("image", "photo", "carousel", "sidecar", "graph")):
        media_type = "image"
    else:
        media_type = "image" if _pick(raw, "thumb") else ""
    return {
        "platform": plat,
        "post_url": url,
        "post_url_norm": util.norm_url(url),
        "handle": ("@" + handle) if handle else "",
        "display_name": str(_pick(raw, "name") or handle or "").strip(),
        "avatar_url": str(_pick(raw, "avatar") or ""),
        "caption": str(_pick(raw, "text") or ""),
        "media_type": media_type,
        "thumb_url": str(_pick(raw, "thumb") or ""),
        "posted_at": posted.isoformat() if posted else "",
        "collected_at": collected.isoformat() if collected else "",
        # The day the post is FILED under. A `day` in the row is the sheet tab's
        # own date and always wins; deriving it from `posted_at` is the legacy
        # fallback and is only trusted inside the sync window (see _fold_scraper).
        "day": util.day_str(given_day) if given_day else (util.day_str(posted) if posted else ""),
        "day_given": bool(given_day),
        "post_id": str(_pick(raw, "post_id") or "").strip(),
        "status": status,
        "status_note": str(_pick(raw, "status_note") or ""),
        "lang": str(_pick(raw, "lang") or "")[:16],
        "likes": util.to_int(_pick(raw, "likes")),
        "comments": util.to_int(_pick(raw, "comments")),
        "shares": util.to_int(_pick(raw, "shares")),
        "views": util.to_int(_pick(raw, "views")),
        "reach": util.to_int(_pick(raw, "reach")),
        "quotes": util.to_int(_pick(raw, "quotes")),
        "bookmarks": util.to_int(_pick(raw, "bookmarks")),
        "author_followers": util.to_int(_pick(raw, "followers")),
        "last_refresh_ms": refreshed_ms,
        "refresh_count": util.to_int(_pick(raw, "refresh_count")),
        "category_raw": str(_pick(raw, "category") or "").strip(),
        "raw": raw,
    }


ARRAY_KEYS = ("items", "posts", "data", "results", "records", "links", "rows")


def normalize_many(body) -> list:
    """A JSON array, or an object holding one under any of ARRAY_KEYS.

    `links` and `rows` are there because the Collector serves its array under
    `rows` for Watch-Tower and adds `items` for us; accepting both means neither
    side can break the other with a rename."""
    arr = body
    if isinstance(body, dict):
        for k in ARRAY_KEYS:
            if isinstance(body.get(k), list):
                arr = body[k]
                break
    if not isinstance(arr, list):
        keys = ", ".join(sorted(body)[:8]) if isinstance(body, dict) else type(body).__name__
        raise ScraperError('Expected a JSON array of posts under one of '
                           f'{"/".join(ARRAY_KEYS)} — the answer had: {keys}')
    out = []
    for item in arr:
        if isinstance(item, dict):
            out.append(normalize(item))
    return out


def build_url(base_url: str, day_from: str, day_to: str) -> str:
    return (base_url.replace("{from}", day_from).replace("{to}", day_to)
            .replace("{date}", day_to))


def _get(url: str, api_key: str, auth_style: str = "bearer", signature: str = "") -> bytes:
    """One GET, with the headers this portal has always sent. Raises
    ScraperError with a plain-English reason a non-programmer can act on."""
    if auth_style.startswith("query:") and api_key:
        param = auth_style.split(":", 1)[1] or "api_key"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({param: api_key})}"
    if not url.lower().startswith(("http://", "https://")):
        raise ScraperError("The scraper URL must start with http:// or https://")
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "VedicReport-portal/1.0"})
    if api_key and auth_style == "bearer":
        req.add_header("Authorization", f"Bearer {api_key}")
    if api_key and auth_style in ("bearer", "x-api-key"):
        req.add_header("X-API-Key", api_key)
    if signature:
        req.add_header("X-Signature-Name", signature)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as r:
            body = r.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        detail = ""
        try:                                    # the Collector puts a sentence in the body
            detail = (json.loads(e.read(8192).decode("utf-8")) or {}).get("error") or ""
        except Exception:
            pass
        raise ScraperError(f"The scraper answered HTTP {e.code} for {url.split('?')[0]}"
                           + (f" — {detail}" if detail else ""))
    except urllib.error.URLError as e:
        raise ScraperError(f"Could not reach the scraper: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise ScraperError(f"The scraper did not answer within {_TIMEOUT}s ({e})")
    if len(body) > _MAX_BYTES:
        raise ScraperError("The scraper's answer is larger than 25 MB — ask for a smaller page (limit=)")
    return body


def fetch_raw(base_url: str, api_key: str, day_from: str, day_to: str,
              auth_style: str = "bearer", signature: str = "") -> bytes:
    """GET the scraper, substituting {from} {to} {date} in the URL first."""
    return _get(build_url(base_url, day_from, day_to), api_key, auth_style, signature)


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ScraperError("The scraper did not return JSON")


def _page_url(url: str, limit: int, offset: int) -> str:
    """The same URL with limit/offset set. Any limit/offset already in it is
    replaced, so the operator's own `?limit=200` sets the page size and we take
    it from there."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if k not in ("limit", "offset")]
    q += [("limit", str(limit)), ("offset", str(offset))]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(q), parts.fragment))


def _limit_of(url: str, default: int = _DEFAULT_LIMIT) -> int:
    for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query):
        if k == "limit":
            try:
                return max(1, min(_DEFAULT_LIMIT, int(v)))
            except ValueError:
                return default
    return default


def _identity(p: dict) -> str:
    return p.get("post_id") or p.get("post_url_norm") or ""


def fetch(source: dict, api_key: str, day_from: str, day_to: str) -> tuple:
    """(posts, body_text) — every page of them.

    A scraper that answers with a bare array, or with an envelope carrying no
    `total`, is fetched once exactly as before. When `total` is present and
    bigger than the first page, the rest are walked by `offset` until they are
    all in. Stops on: `total` reached, an empty page, a page that repeats what
    we already have, `total` changing under us, or the hard caps below — the
    short read is kept rather than thrown away, because every post we did get
    is a post whose numbers are now current, and a post nobody sent is simply
    left untouched.
    """
    base = build_url(source["base_url"], day_from, day_to)
    auth_style = source.get("auth_style") or "bearer"
    signature = source.get("signature_name") or ""

    body = _get(base, api_key, auth_style, signature)
    data = _json(body)
    posts = normalize_many(data)
    total = data.get("total") if isinstance(data, dict) else None
    if not isinstance(total, int) or not posts or len(posts) >= total:
        return posts, body.decode("utf-8", "replace")

    limit = _limit_of(base, len(posts))
    seen = {_identity(p) for p in posts}
    bytes_read, pages = len(body), 1
    while (len(posts) < total and pages < _MAX_PAGES and len(posts) < _MAX_ROWS
           and bytes_read < _MAX_TOTAL_BYTES):
        body = _get(_page_url(base, limit, len(posts)), api_key, auth_style, signature)
        bytes_read += len(body)
        pages += 1
        data = _json(body)
        page = normalize_many(data)
        if not page:
            break
        fresh = [p for p in page if _identity(p) not in seen]
        if not fresh:
            break                                   # the same page again — stop rather than spin
        seen.update(_identity(p) for p in fresh)
        posts.extend(fresh)
        if isinstance(data.get("total") if isinstance(data, dict) else None, int) \
           and data["total"] != total:
            break                                   # the watchlist changed mid-walk; keep what we have
    return posts, json.dumps([p["raw"] for p in posts], ensure_ascii=False)


def probe(source: dict, api_key: str) -> dict:
    """The handshake — `client_sources.probe_url`, e.g.
    `https://scraper.vedictech.in/api/project?project=16`. Returns the parsed
    body so Admin -> Clients can show which project and which sheet a key is
    actually wired to, BEFORE a sync files one client's posts under another."""
    url = (source.get("probe_url") or "").strip()
    if not url:
        raise ScraperError("No handshake URL is set for this source.")
    data = _json(_get(url, api_key, source.get("auth_style") or "bearer",
                      source.get("signature_name") or ""))
    if not isinstance(data, dict):
        raise ScraperError("The handshake did not return a JSON object.")
    return data

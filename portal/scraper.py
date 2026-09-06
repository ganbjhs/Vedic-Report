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
    "name":         ["name", "display_name", "full_name", "author_name", "page_name",
                     "user.full_name", "user.name", "author.name", "owner.name"],
    "handle":       ["handle", "username", "screen_name", "user.username", "author.username",
                     "owner.username", "account"],
    "avatar":       ["avatar", "avatar_url", "profile_pic_url", "profile_image_url",
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
    "category":     ["category", "classification", "section", "label", "bucket"],
    "media_type":   ["media_type", "type", "media.0.type", "media.0.media_type", "product_type"],
    "thumb":        ["thumbnail_url", "thumbnail", "thumb", "media.0.thumbnail_url", "media.0.url",
                     "image_url", "display_url", "preview_url"],
}

_TIMEOUT = 20
_MAX_BYTES = 25 * 1024 * 1024


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
    plat = util.platform_of(url, str(_pick(raw, "platform") or ""))
    handle = str(_pick(raw, "handle") or "").strip().lstrip("@")
    posted = util.parse_when(_pick(raw, "posted_at"))
    collected = util.parse_when(_pick(raw, "collected_at"))
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
        "day": util.day_str(posted) if posted else "",
        "likes": util.to_int(_pick(raw, "likes")),
        "comments": util.to_int(_pick(raw, "comments")),
        "shares": util.to_int(_pick(raw, "shares")),
        "views": util.to_int(_pick(raw, "views")),
        "reach": util.to_int(_pick(raw, "reach")),
        "category_raw": str(_pick(raw, "category") or "").strip(),
        "raw": raw,
    }


def normalize_many(body) -> list:
    """A JSON array, or an object holding one under posts/data/items/results."""
    arr = body
    if isinstance(body, dict):
        for k in ("posts", "data", "items", "results", "records"):
            if isinstance(body.get(k), list):
                arr = body[k]
                break
    if not isinstance(arr, list):
        raise ScraperError('Expected a JSON array of posts (or {"posts": [...]}).')
    out = []
    for item in arr:
        if isinstance(item, dict):
            out.append(normalize(item))
    return out


def build_url(base_url: str, day_from: str, day_to: str) -> str:
    return (base_url.replace("{from}", day_from).replace("{to}", day_to)
            .replace("{date}", day_to))


def fetch_raw(base_url: str, api_key: str, day_from: str, day_to: str,
              auth_style: str = "bearer", signature: str = "") -> bytes:
    """GET the scraper. Raises ScraperError with a plain-English reason."""
    url = build_url(base_url, day_from, day_to)
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
        raise ScraperError(f"The scraper answered HTTP {e.code} for {url.split('?')[0]}")
    except urllib.error.URLError as e:
        raise ScraperError(f"Could not reach the scraper: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise ScraperError(f"The scraper did not answer within {_TIMEOUT}s ({e})")
    if len(body) > _MAX_BYTES:
        raise ScraperError("The scraper's answer is larger than 25 MB — narrow the date range")
    return body


def fetch(source: dict, api_key: str, day_from: str, day_to: str) -> tuple:
    """(posts, body_text). `source` is a `client_sources` row (dict-like)."""
    body = fetch_raw(source["base_url"], api_key, day_from, day_to,
                     auth_style=source.get("auth_style") or "bearer",
                     signature=source.get("signature_name") or "")
    try:
        text = body.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ScraperError("The scraper did not return JSON")
    return normalize_many(data), text

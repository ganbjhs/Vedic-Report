"""
URL extraction + classification (platform, kind = post/comment/other).
Pure functions, no browser needed. Extend PLATFORMS to teach it new sites.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs, urlunparse

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
             "igsh", "igshid", "fbclid", "mibextid", "rcm", "s", "t", "si"}


def clean_url(url: str) -> str:
    """Strip tracking params + fragments; keep params that carry meaning (comment ids etc.)."""
    p = urlparse(url.strip())
    q = parse_qs(p.query, keep_blank_values=True)
    q = {k: v for k, v in q.items() if k.lower() not in _TRACKING}
    query = "&".join(f"{k}={v[0]}" if v and v[0] != "" else k for k, v in q.items())
    netloc = p.netloc.lower()
    for pre in ("www.", "m.", "mobile."):
        if netloc.startswith(pre):
            netloc = netloc[len(pre):]
            break
    return urlunparse((p.scheme or "https", netloc, p.path.rstrip("/") or "/", "", query, ""))


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    for pre in ("www.", "m.", "mobile.", "web.", "l."):
        if h.startswith(pre):
            h = h[len(pre):]
    return h


# Each rule: (platform, host predicate, [(kind, regex on full url)]) — first match wins.
PLATFORMS = [
    ("instagram", lambda h: "instagram.com" in h, [
        ("comment", r"/(p|reel|tv)/[^/]+/c/\d+"),
        ("post",    r"/(p|reel|reels|tv)/[^/?#]+"),
        ("story",   r"/stories/"),
        ("profile", r"^https?://[^/]+/[^/?#]+/?$"),
    ]),
    ("twitter", lambda h: h in ("x.com", "twitter.com", "t.co") or h.endswith(".twitter.com"), [
        ("post",    r"/status(es)?/\d+"),
        ("profile", r"^https?://[^/]+/[^/?#]+/?$"),
    ]),
    ("facebook", lambda h: "facebook.com" in h or h == "fb.watch" or h == "fb.com", [
        ("comment", r"[?&](comment_id|reply_comment_id)="),
        ("post",    r"(/posts/|/photos?/|/videos?/|/reels?/|/share/(p|r|v)/|permalink\.php|story\.php|story_fbid=|/watch|fb\.watch|/groups/[^/]+/(posts|permalink)/)"),
        ("profile", r"^https?://[^/]+/[^/?#]+/?$"),
    ]),
    ("linkedin", lambda h: "linkedin.com" in h or h == "lnkd.in", [
        ("comment", r"(commentUrn=|/comments?/|highlightedUpdateUrn=)"),
        ("post",    r"(/posts/|/feed/update/|urn:li:(activity|share|ugcPost)|/pulse/|lnkd\.in/)"),
        ("profile", r"/in/[^/?#]+"),
    ]),
    ("youtube", lambda h: "youtube.com" in h or h == "youtu.be", [
        ("comment", r"[?&]lc="),
        ("post",    r"(watch\?v=|youtu\.be/|/shorts/|/live/|/post/)"),
        ("profile", r"(/@|/channel/|/c/)"),
    ]),
    ("threads", lambda h: "threads.net" in h or "threads.com" in h, [
        ("post",    r"/post/[^/?#]+"),
        ("profile", r"^https?://[^/]+/@[^/?#]+/?$"),
    ]),
    ("tiktok", lambda h: "tiktok.com" in h, [
        ("comment", r"[?&]comment_id="),
        ("post",    r"(/video/\d+|/t/|vm\.tiktok)"),
        ("profile", r"/@[^/?#]+/?$"),
    ]),
    ("reddit", lambda h: "reddit.com" in h or h == "redd.it", [
        ("comment", r"/comments/[^/]+/[^/]+/[a-z0-9]+"),
        ("post",    r"/comments/[^/]+"),
    ]),
]


def classify(url: str) -> dict:
    """
    -> {"url", "clean_url", "platform", "kind"}  kind ∈ post|comment|story|profile|other
    """
    h = _host(url)
    platform, kind = "other", "other"
    for name, pred, rules in PLATFORMS:
        if pred(h):
            platform = name
            for k, rx in rules:
                if re.search(rx, url, re.I):
                    kind = k
                    break
            break
    return {"url": url, "clean_url": clean_url(url), "platform": platform, "kind": kind}


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+?(?=[.,;:!?]*(?:\s|$|[<>\"')\]]))", re.I)


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(_URL_RE.findall(text or "")))

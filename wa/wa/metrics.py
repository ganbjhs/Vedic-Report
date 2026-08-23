"""
Best-effort public engagement metrics for a link (likes / comments / shares / views).

IMPORTANT – read before relying on this:
  * WhatsApp knows nothing about likes; these are scraped from the *source*
    platform. Every platform hides or changes this regularly, and scraping
    generally violates their ToS. Treat numbers as approximate and expect
    breakage; the rest of the toolkit works fine without this module.
  * "Reach"/impressions are only visible to the post owner on every major
    platform, so they are NOT retrievable for other people's posts.
  * Instagram / Facebook / LinkedIn / Threads need you to be logged in inside
    the same Chromium profile (just log in once in a normal tab of the
    automation browser). Twitter/X uses a public endpoint (no login).
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

EMPTY = {"likes": "", "comments": "", "shares": "", "views": "", "metrics_note": ""}


def _num(s: str | None) -> str:
    """'1,234' / '12.5K' / '3M' -> int-as-string, else ''."""
    if not s:
        return ""
    s = s.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KkMmBb]?)$", s)
    if not m:
        return ""
    v = float(m.group(1))
    v *= {"k": 1e3, "m": 1e6, "b": 1e9, "": 1}[m.group(2).lower()]
    return str(int(v))


# ---------------------------------------------------------------- twitter
def _twitter(url: str) -> dict:
    import requests
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if not m:
        return {**EMPTY, "metrics_note": "no tweet id"}
    r = requests.get("https://cdn.syndication.twimg.com/tweet-result",
                     params={"id": m.group(1), "token": "x", "lang": "en"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    if r.status_code != 200 or not r.text.strip():
        return {**EMPTY, "metrics_note": f"syndication http {r.status_code}"}
    d = r.json()
    return {"likes": str(d.get("favorite_count", "")),
            "comments": str(d.get("conversation_count", "")),
            "shares": "", "views": "", "metrics_note": "x syndication (retweets/views not exposed)"}


# --------------------------------------------------- browser-based scrapers
def _og_desc(page) -> str:
    try:
        return page.evaluate("""() => {
          const m = document.querySelector('meta[property="og:description"], meta[name="description"]');
          return m ? m.getAttribute('content') : ''; }""") or ""
    except Exception:
        return ""


def _instagram(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    desc = _og_desc(page)
    likes = re.search(r"([\d.,]+[KkMm]?)\s+likes?", desc)
    comments = re.search(r"([\d.,]+[KkMm]?)\s+comments?", desc)
    views = re.search(r"([\d.,]+[KkMm]?)\s+(views|plays)", desc, re.I)
    if not (likes or comments):
        return {**EMPTY, "metrics_note": "instagram: not exposed (login? private? layout changed)"}
    return {"likes": _num(likes and likes.group(1)), "comments": _num(comments and comments.group(1)),
            "shares": "", "views": _num(views and views.group(1)), "metrics_note": "instagram og:description"}


def _facebook(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    html = page.content()
    likes = re.search(r'"reaction_count":\{"count":(\d+)', html) or re.search(r'"i18n_reaction_count":"([\d.,KM]+)"', html)
    comments = re.search(r'"comment_count":\{"total_count":(\d+)', html) or re.search(r'"comments":\{"total_count":(\d+)', html)
    shares = re.search(r'"share_count":\{"count":(\d+)', html)
    views = re.search(r'"video_view_count":(\d+)', html) or re.search(r'"play_count":(\d+)', html)
    if not any([likes, comments, shares]):
        return {**EMPTY, "metrics_note": "facebook: not exposed (login? private? layout changed)"}
    return {"likes": _num(likes and likes.group(1)), "comments": _num(comments and comments.group(1)),
            "shares": _num(shares and shares.group(1)), "views": _num(views and views.group(1)),
            "metrics_note": "facebook page json"}


def _linkedin(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    html = page.content()
    likes = re.search(r'data-num-reactions="(\d+)"', html) or re.search(r'"numLikes":(\d+)', html) \
        or re.search(r'aria-label="(\d[\d,]*) reactions?"', html)
    comments = re.search(r'data-num-comments="(\d+)"', html) or re.search(r'"numComments":(\d+)', html) \
        or re.search(r'(\d[\d,]*) comments?', html)
    shares = re.search(r'"numShares":(\d+)', html) or re.search(r'(\d[\d,]*) reposts?', html)
    views = re.search(r'"numViews":(\d+)', html) or re.search(r'(\d[\d,]*) impressions?', html)
    if not any([likes, comments]):
        return {**EMPTY, "metrics_note": "linkedin: not exposed (login? layout changed)"}
    return {"likes": _num(likes and likes.group(1)), "comments": _num(comments and comments.group(1)),
            "shares": _num(shares and shares.group(1)), "views": _num(views and views.group(1)),
            "metrics_note": "linkedin page"}


def _youtube(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    html = page.content()
    views = re.search(r'"viewCount":"(\d+)"', html)
    likes = re.search(r'"likeCount":"?(\d+)', html) or re.search(r'like this video along with ([\d,]+) other', html) \
        or re.search(r'"accessibilityText":"([\d.,KM]+) likes?', html)
    comments = re.search(r'"commentCount":\{"simpleText":"([\d.,KM]+)"', html) \
        or re.search(r'"contextualInfo":\{"runs":\[\{"text":"([\d.,KM]+)"', html)
    if not views:
        return {**EMPTY, "metrics_note": "youtube: not parsed"}
    return {"likes": _num(likes and likes.group(1)), "comments": _num(comments and comments.group(1)),
            "shares": "", "views": _num(views.group(1)), "metrics_note": "youtube page"}


def _threads(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    desc = _og_desc(page)
    likes = re.search(r"([\d.,]+[KkMm]?)\s+likes?", desc)
    comments = re.search(r"([\d.,]+[KkMm]?)\s+(replies|comments?)", desc)
    if not (likes or comments):
        return {**EMPTY, "metrics_note": "threads: not exposed"}
    return {"likes": _num(likes and likes.group(1)), "comments": _num(comments and comments.group(1)),
            "shares": "", "views": "", "metrics_note": "threads og:description"}


BROWSER_SCRAPERS = {"instagram": _instagram, "facebook": _facebook, "linkedin": _linkedin,
                    "youtube": _youtube, "threads": _threads}


def fetch_metrics(platform: str, url: str, page=None) -> dict:
    """Return dict with likes/comments/shares/views/metrics_note (strings; '' if unknown)."""
    try:
        if platform == "twitter":
            return _twitter(url)
        fn = BROWSER_SCRAPERS.get(platform)
        if fn and page is not None:
            return fn(page, url)
        return {**EMPTY, "metrics_note": f"{platform}: no scraper"}
    except Exception as e:  # noqa: BLE001
        return {**EMPTY, "metrics_note": f"{platform}: error {type(e).__name__}: {e}"[:200]}

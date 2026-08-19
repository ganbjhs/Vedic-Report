#!/usr/bin/env python3
"""Read the engagement metadata of X posts — likes, reposts, replies, views.

    python metrics/x_metrics.py input.xlsx --json metrics.json --csv metrics.csv
    python metrics/x_metrics.py links.txt
    python metrics/x_metrics.py https://x.com/user/status/123 https://x.com/…

WHY THIS EXISTS. The sheet the team fills in carries Like / Impressions / Views
columns that a human types by hand, and the combined styles print exactly what
is typed. Everything needed to read those numbers off the post itself already
existed inside the influencer capture — but only as a side effect of taking a
screenshot, which costs a full render, a media wait and a quality re-take per
link. This module is the same reader without the camera: load, find the post,
read the action bar, move on.

NOTHING HERE IS NEW LOGIC. `influencer/inf_capture.py` is imported and its
`_load_tweet`, `_pick_article`, `read_metrics` and `_read_handle` are called as
they stand, so a fix to the parsing lands in both places at once. The one thing
added is bookmarks, which the report never asked for before.

Progress goes out through `profiles/progress.py`, never `print()` by hand: the
web layer regex-matches these lines to drive the job page, and
`profiles/tests/test_progress_contract.py` holds both sides to it.

Output — one dict per link, in input order:

    {"link", "status", "handle", "posted_at", "likes", "reposts", "replies",
     "views", "bookmarks", "display": {…}}

`status` is inf_capture's: "ok" | "login_wall" | "not_found" | "error: …".
A number that could not be read is None, and its display string is "—" —
never 0, because "nobody liked it" and "X did not tell us" are different facts
and a report that confuses them is worse than one that admits the gap.

LOGGED OUT, X SHOWS NO VIEW COUNT and often no counts at all, so the saved
`sessions/x_state.json` is used when present. Without it the run still works and
simply reports what a logged-out visitor can see.
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("src", "influencer", "profiles"):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import inf_capture                                        # noqa: E402
import progress as P                                      # profiles/, noqa: E402

MISSING = inf_capture.MISSING

# Bookmarks are in the same aria-label as the rest; the influencer report has
# never printed them, so the pattern lives here rather than in the shared file.
_BOOKMARKS = re.compile(inf_capture._NUM + r"\s*(?:bookmarks|bookmark)\b", re.I)

# The four keys inf_capture returns, in this module's vocabulary. Keeping both
# names is deliberate: `reach` is what the influencer report calls views, and
# the sheet's own column is headed "Reach/views".
_RENAME = {"reactions": "likes", "comments": "replies",
           "shares": "reposts", "reach": "views"}

CTX_KWARGS = {
    "viewport": {"width": 1280, "height": 1600},
    "locale": "en-IN",
    "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
def links_from(path_or_urls) -> list:
    """Links from an .xlsx / .csv / .txt file, or from bare URLs on argv."""
    urls = [u for u in path_or_urls if str(u).lower().startswith("http")]
    if urls:
        return urls                                # order kept, no dedupe
    out = []
    for item in path_or_urls:
        p = Path(item)
        if not p.exists():
            print(f"[metrics] no such file: {p}", flush=True)
            continue
        if p.suffix.lower() in (".xlsx", ".xlsm"):
            import input_loader                            # src/, frozen
            out += [r["link"] for r in input_loader.load_excel(str(p))]
        else:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                for m in re.findall(r"https?://\S+", line):
                    if is_x_url(m):
                        out.append(m.rstrip(".,;:!?)]}>\"'"))
    return out


def is_x_url(url: str) -> bool:
    u = (url or "").lower()
    return "x.com/" in u or "twitter.com/" in u


def storage_state():
    f = ROOT / "sessions" / "x_state.json"
    if f.exists():
        return str(f)
    P.metrics_no_session()
    return None


# --------------------------------------------------------------------------- #
# Reading one post
# --------------------------------------------------------------------------- #
def _posted_at(tweet) -> str:
    """The post's own timestamp, ISO-8601, straight off <time datetime=…>."""
    try:
        el = tweet.locator("time").first
        if el.count():
            return (el.get_attribute("datetime") or "").strip()
    except Exception:
        pass
    return ""


def _bookmarks(tweet):
    try:
        groups = tweet.locator('[role="group"]')
        for i in range(min(groups.count(), 6)):
            m = _BOOKMARKS.search(groups.nth(i).get_attribute("aria-label") or "")
            if m:
                return inf_capture._to_int(m.group(1))
    except Exception:
        pass
    return None


def _blank(link: str, status: str) -> dict:
    row = {"link": link, "status": status, "handle": "", "posted_at": "",
           "likes": None, "reposts": None, "replies": None, "views": None,
           "bookmarks": None}
    row["display"] = {k: MISSING for k in
                      ("likes", "reposts", "replies", "views", "bookmarks")}
    return row


def read_one(page, link: str) -> dict:
    """Metadata for one X post. Never raises for content — see `status`."""
    try:
        status = inf_capture._load_tweet(page, link)
    except Exception as e:
        return _blank(link, f"error: {e}")
    if status != "ok":
        return _blank(link, status)

    try:
        tweet, _ = inf_capture._pick_article(page, link)
        raw = (inf_capture.read_metrics(tweet) or {}).get("_raw") or {}
        row = _blank(link, "ok")
        for src, dest in _RENAME.items():
            row[dest] = raw.get(src)
        row["bookmarks"] = _bookmarks(tweet)
        row["handle"] = inf_capture._read_handle(tweet)
        row["posted_at"] = _posted_at(tweet)
    except Exception as e:
        return _blank(link, f"error: {e}")

    row["display"] = {k: inf_capture.compact(row[k]) for k in
                      ("likes", "reposts", "replies", "views", "bookmarks")}
    return row


def read_links(links: list, headless: bool = True, pause: float = 0.6) -> list:
    """One browser, one context, one page, every link in order.

    Sequential on purpose. This is a read, not a capture: it is cheap enough
    that parallel browsers would buy seconds while spending the X account's
    daily budget faster (RULEBOOK rule 21), and that account is the scarce
    resource in this project, not CPU.
    """
    from playwright.sync_api import sync_playwright

    links = [u for u in links if is_x_url(u)]
    if not links:
        print("[metrics] no X links to read.", flush=True)
        return []

    P.metrics_reading(len(links))
    state = storage_state()
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        kwargs = dict(CTX_KWARGS)
        if state:
            kwargs["storage_state"] = state
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        for n, link in enumerate(links, start=1):
            row = read_one(page, link)
            rows.append(row)
            d = row["display"]
            P.metrics_one(n, len(links), row["status"], d["likes"], d["reposts"],
                          d["replies"], d["views"], link)
            if pause:
                time.sleep(pause)
        browser.close()

    unread = sum(1 for r in rows if r["status"] != "ok")
    partial = sum(1 for r in rows if r["status"] == "ok" and
                  any(r[k] is None for k in ("likes", "reposts", "replies", "views")))
    if unread:
        P.metrics_unread(unread)
    if partial:
        P.metrics_partial(partial)
    return rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
_CSV_COLUMNS = ("link", "handle", "posted_at", "status",
                "likes", "reposts", "replies", "views", "bookmarks")


def write_json(rows: list, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"[metrics] wrote {dest}", flush=True)


def write_csv(rows: list, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c.replace("_", " ").title() for c in _CSV_COLUMNS])
        for r in rows:
            w.writerow([r.get(c) if r.get(c) is not None else "" for c in _CSV_COLUMNS])
    print(f"[metrics] wrote {dest}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read likes / reposts / replies / views for X posts.")
    ap.add_argument("input", nargs="+",
                    help="an .xlsx or .txt of links, or the links themselves")
    ap.add_argument("--json", dest="json_out", default="",
                    help="write the full result here (default: metrics.json "
                         "beside the input)")
    ap.add_argument("--csv", dest="csv_out", default="",
                    help="also write a spreadsheet-friendly CSV here")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser (debugging)")
    args = ap.parse_args()

    links = links_from(args.input)
    if not links:
        print("[metrics] nothing to read.", flush=True)
        sys.exit(1)

    rows = read_links(links, headless=not args.headed)
    first = Path(args.input[0])
    default_json = (first.with_name("metrics.json")
                    if first.exists() else Path("metrics.json"))
    write_json(rows, Path(args.json_out) if args.json_out else default_json)
    if args.csv_out:
        write_csv(rows, Path(args.csv_out))


if __name__ == "__main__":
    main()

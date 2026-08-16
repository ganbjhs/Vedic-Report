"""Which network a link belongs to, and a platform-neutral row reader.

The frozen `src/input_loader._rows_from_grid` is X-only by construction (its
last line drops every non-X link). A second platform needs the SAME sheet
semantics — header detection, the plain-list mode where a URL-less line is a
category, comment lines, sibling-cell account names — minus that one filter.
So the layout logic is reused **read-only** (`_header_index`, `_clean_url`,
`_URL_RE`, `_IGNORE_HEADERS` are imported, never copied or edited) and only the
keep-test is parameterised.

Used by both `prof_runner` (what the job captures) and `webapp/uploads.analyse`
(what the preview shows), so the two cannot disagree about what a Facebook link
is. Nothing here imports Playwright.
"""
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
import input_loader          # noqa: E402  frozen, read-only

_FB_HOSTS = ("facebook.com", "fb.com", "fb.watch", "m.facebook.com",
             "mbasic.facebook.com", "web.facebook.com")

# What a Facebook *post* URL looks like. Anything on facebook.com that is not
# one of these (a Page home, /marketplace, /groups/<id> with no post) is not
# something a single-post capture can frame.
_FB_POST_RE = re.compile(
    r"facebook\.com/(?:"
    r"[^/?#]+/posts/[^/?#]+"            # /<page>/posts/<id or pfbid>
    r"|[^/?#]+/photos/[^/?#]+/\d+"      # /<page>/photos/a.xxx/<id>
    r"|photo(?:\.php)?/?\?.*fbid=\d+"   # /photo/?fbid=…  /photo.php?fbid=…
    r"|permalink\.php\?.*story_fbid="   # /permalink.php?story_fbid=…&id=…
    r"|story\.php\?.*story_fbid="       # mobile permalink
    r"|[^/?#]+/videos/[^/?#]+"          # /<page>/videos/<id>
    r"|watch/?\?.*v=\d+"                # /watch/?v=…
    r"|reel/\d+"                        # /reel/<id>
    r"|share/[pv]/[^/?#]+"              # /share/p/<code>  share links
    r"|groups/[^/?#]+/(?:posts|permalink)/[^/?#]+"
    r")", re.I)


def is_x_url(url: str) -> bool:
    return input_loader.is_x_url(url)


def is_fb_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _FB_HOSTS) or "fb.watch/" in u


def is_fb_post_url(url: str) -> bool:
    u = (url or "").lower()
    return "fb.watch/" in u or bool(_FB_POST_RE.search(u))


def normalize_fb_url(url: str) -> str:
    """Desktop host, no tracking junk. m./mbasic./web. all serve the same post;
    the desktop layout is the one the capture is written against."""
    u = input_loader._clean_url(url)
    u = re.sub(r"^https?://(m|mbasic|web)\.facebook\.com", "https://www.facebook.com", u, flags=re.I)
    u = re.sub(r"^https?://facebook\.com", "https://www.facebook.com", u, flags=re.I)
    if "?" in u and "fbid=" not in u and "story_fbid=" not in u and "v=" not in u:
        u = u.split("?", 1)[0]          # /posts/<id>?__cft__=… → /posts/<id>
    return u


MATCHERS = {"x": is_x_url, "facebook": is_fb_url}


def fb_name(url: str) -> str:
    """'<page>' from /<page>/posts/… when present, else 'Facebook post'."""
    m = re.search(r"facebook\.com/([^/?#]+)/(?:posts|photos|videos)/", (url or "").lower())
    if m and m.group(1) not in ("photo", "watch", "reel", "share", "groups", "permalink.php"):
        return m.group(1)
    return "Facebook post"


def rows_from_grid(grid, platform: str = "x") -> list:
    """The frozen reader's rows for X; the same layout rules for any other
    platform, keeping that platform's links instead. Rows carry the platform."""
    if platform == "x":
        return input_loader._rows_from_grid(grid)
    keep = MATCHERS[platform]
    grid = [cells for cells in grid if any(cells)]
    if not grid:
        return []
    header = input_loader._header_index(grid)
    rows = []

    def row(category, account, link):
        link = input_loader._clean_url(link)
        if platform == "facebook":
            link = normalize_fb_url(link)
        return {"category": (category or "Uncategorized").strip() or "Uncategorized",
                "account_name": (account or "").strip() or fb_name(link),
                "link": link, "post_link": link, "platform": platform}

    if header:
        start, cmap = header
        for cells in grid[start + 1:]:
            link = cells[cmap["link"]] if cmap["link"] < len(cells) else ""
            if not input_loader._URL_RE.search(link):
                link = next((c for c in cells if input_loader._URL_RE.search(c)), "")
            if not link:
                continue
            account = cells[cmap["account"]] if cmap["account"] is not None \
                and cmap["account"] < len(cells) else ""
            category = cells[cmap["category"]] if cmap["category"] is not None \
                and cmap["category"] < len(cells) else ""
            rows.append(row(category, account, link))
    else:
        category = "Uncategorized"
        for cells in grid:
            if cells and cells[0].lstrip().startswith("#"):
                continue
            url = next((c for c in cells if input_loader._URL_RE.search(c)), "")
            if url:
                account = next((c for c in cells if c and not input_loader._URL_RE.search(c)
                                and not re.fullmatch(r"[\d,]+", c)), "")
                rows.append(row(category, account, url))
            else:
                joined = " ".join(c for c in cells if c)
                if joined.lower() not in input_loader._IGNORE_HEADERS:
                    category = joined or category

    kept = [r for r in rows if keep(r["link"])]
    dropped = len(rows) - len(kept)
    if dropped:
        print(f"[input] skipped {dropped} non-{platform} link(s)", flush=True)
    return kept


def load_rows(source: str, platform: str = "x") -> list:
    """Rows from an .xlsx (or a plain text list) for `platform`. X goes through
    the frozen loader unchanged; other platforms read the same file shapes."""
    if platform == "x":
        return input_loader.load(source)
    path = Path(source)
    print(f"[input] reading {source}", flush=True)
    grid = None
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(str(path), read_only=True, data_only=True)
            ws = wb.active
            grid = [[("" if c is None else str(c)).strip() for c in r]
                    for r in ws.iter_rows(values_only=True)]
            wb.close()
        except Exception:
            grid = None
    if grid is None:
        text = path.read_text(encoding="utf-8", errors="replace")
        grid = [[c.strip() for c in re.split(r"[\t,]", line)] for line in text.splitlines()]
    return rows_from_grid(grid, platform)

"""Understand a Google Sheet the team keeps by hand — whatever shape it is in.

The sheets are dynamic. In one workbook we have seen, side by side:

  * one tab per DAY, named "17/8/26", one column: yellow section headings
    ("3 Party Pages Posting", "National X Influencers") with links beneath and
    blank rows between;
  * a "Tweet Links" tab: date headings down column A ("Date- 4-7-26 Sr No."),
    links in column B, an unnamed number in column C, new dates appended below;
  * a "Counter Links" tab where ONE cell holds several links on separate lines;
  * a "3rd Party Posting" tab with a proper header ("Date", "Link") and links
    written without https://.

Nobody should have to say "links are in column B" — and by next week they may
not be. So this module reads the sheet the way a person does: every URL in any
cell is a post (a cell with several lines is several posts), text without a URL
above a run of links is the section they belong to, anything that parses as a
date sets the date for what follows, a numeric cell in a link's row is a
metric (named by the header above it when there is one), and a short text cell
in a link's row is the account name. The result is a canonical grid
`Section | Handle | Link | <metric…>` — exactly what the existing readers
(`netlinks.rows_from_grid` via `uploads.analyse`) already understand, so the
capture pipeline never sees anything new.

It also lists a workbook's TABS (from the public htmlview page — no API, no
OAuth) and knows which tab, or which block inside a tab, is the newest date.
That is what "run it whenever a new date appears" is built on (`sources.py`).

Grok (xAI) is an OPTIONAL second opinion, only when `XAI_API_KEY` is set and
the heuristics come back with a shape they are not sure of (no header, an
unnamed number column, or zero links where cells clearly hold something). It
is asked one question — "name these columns" — and its answer is used only to
label metric columns; it never adds, drops or reorders a link. Everything works
with the key unset.
"""
import datetime as _dt
import json
import re
import urllib.request

from . import config, sheets

# --------------------------------------------------------------------------- #
# URLs, dates, headers
# --------------------------------------------------------------------------- #
_URL = re.compile(
    r"(https?://[^\s\"'<>()\[\]]+"
    r"|(?<![\w.])(?:www\.)?(?:x\.com|twitter\.com|facebook\.com|fb\.watch|m\.facebook\.com"
    r"|instagram\.com|instagr\.am|youtube\.com|youtu\.be)/[^\s\"'<>()\[\]]+)",
    re.I)
_TRAIL = ".,;:!?)]}\"'"

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})(?!\d)")
_DATE_TXT = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s*[-/ ]?\s*(" + "|".join(_MONTHS) +
                       r")[a-z]*\.?\s*[-/, ]?\s*(\d{2,4})?(?!\d)", re.I)
_DATE_ISO = re.compile(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)")

# Header vocabulary. Kept in sync with profiles/netlinks.py by construction:
# the canonical grid this module writes uses the names that reader recognises.
_LINK_HDR = ("link", "links", "url", "urls", "post link", "post url", "post", "tweet", "tweet link")
_ACCT_HDR = ("handle", "handle name", "account", "account name", "page", "page name",
             "name", "author", "profile", "username", "user")
_SECT_HDR = ("section", "category", "group", "type", "bucket", "topic", "campaign")
_DATE_HDR = ("date", "day", "posted", "posted on", "dated")
_METRIC_HDR = {
    "Like": ("like", "likes", "reactions", "reaction", "hearts"),
    "Impressions": ("impression", "impressions", "post impression", "post impressions", "imp"),
    "Views": ("views", "view", "video views", "plays", "reach/views", "reachviews"),
    "Reach": ("reach", "post reach"),
    "Comments": ("comments", "comment", "replies"),
    "Shares": ("shares", "share", "reposts", "retweets", "rt"),
    "Followers": ("followers", "follower"),
}
_IGNORE_TEXT = {"sr no", "sr no.", "s no", "sno", "sl no", "serial", "#", "no", "no.", "sr", "total",
                "grand total", "count", "remarks", "remark", "status", "done", "pending"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/ ]+", " ", (s or "").lower())).strip()


def urls_in(cell: str) -> list:
    """Every post URL in one cell — several lines / several links included."""
    out = []
    for m in _URL.finditer(cell or ""):
        u = m.group(0).rstrip(_TRAIL)
        if not u.lower().startswith("http"):
            u = "https://" + u
        # a cell like "https://x.com/a/status/1https://x.com/b/status/2" (two
        # links pasted with no separator) — split at the second scheme
        parts = re.split(r"(?=https?://)", u)
        out.extend(p.rstrip(_TRAIL) for p in parts if p.strip())
    return out


def parse_date(text: str, default_year: int = None):
    """A date found in free text ('17/8/26', 'Date- 4-7-26 Sr No.', '14 Jul',
    '2026-08-17') or None. Day-first, as the team writes them."""
    t = (text or "").strip()
    if not t:
        return None
    m = _DATE_ISO.search(t)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _DATE_NUM.search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if mo > 12 and d <= 12:           # someone wrote month/day
            d, mo = mo, d
        try:
            return _dt.date(y, mo, d)
        except ValueError:
            return None
    m = _DATE_TXT.search(t)
    if m:
        d = int(m.group(1))
        mo = _MONTHS.index(m.group(2).lower()[:3]) + 1
        y = int(m.group(3)) if m.group(3) else (default_year or _dt.date.today().year)
        if y < 100:
            y += 2000
        try:
            return _dt.date(y, mo, d)
        except ValueError:
            return None
    return None


def _is_number(cell: str) -> bool:
    c = (cell or "").strip().replace(",", "").replace("%", "")
    if not c:
        return False
    c = re.sub(r"(?i)[kKmM]$", "", c)
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", c))


def _has_letters(cell: str) -> bool:
    return bool(re.search(r"[A-Za-zऀ-ॿ]", cell or ""))


def _header_roles(cells: list) -> dict:
    """{col: role} when this row reads like a header row, else {}. Roles:
    link | account | section | date | metric:<Name>."""
    roles, hits = {}, 0
    for j, c in enumerate(cells):
        n = _norm(c)
        if not n:
            continue
        if n in _LINK_HDR:
            roles[j] = "link"; hits += 1
        elif n in _ACCT_HDR:
            roles[j] = "account"; hits += 1
        elif n in _SECT_HDR:
            roles[j] = "section"; hits += 1
        elif n in _DATE_HDR or n.startswith("date"):
            roles[j] = "date"; hits += 1
        else:
            for name, alts in _METRIC_HDR.items():
                if n in alts:
                    roles[j] = f"metric:{name}"; hits += 1
                    break
    # A header needs a link column OR two recognised names; a lone "Date-4-7-26"
    # heading with "Links" beside it also counts (that is the Tweet Links tab).
    if "link" in roles.values() or hits >= 2:
        return roles
    return {}


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
def understand(grid: list, default_year: int = None) -> dict:
    """Read any grid into posts + a canonical grid.

    Returns {
      "posts": [{section, date, link, account, metrics{Name: value}}],
      "grid": canonical grid (header + rows) for uploads.analyse,
      "sections": [...], "dates": [iso...], "latest_date": iso|None,
      "metric_names": [...], "unnamed_numbers": int, "shape": str,
      "notes": [human-readable observations]
    }
    """
    grid = [[str(c) if c is not None else "" for c in row] for row in (grid or [])]
    posts, notes = [], []
    roles, header_at = {}, None
    section, date = None, None
    unnamed_numbers = 0
    named_metric_cols = {}          # col -> Name (from header)

    for i, cells in enumerate(grid):
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        # header row? (only before the first link is seen, or when a later
        # header restates the columns — both happen in these sheets)
        found_urls = [(j, urls_in(c)) for j, c in enumerate(cells)]
        row_urls = [(j, u) for j, us in found_urls for u in us]
        if not row_urls:
            hr = _header_roles(cells)
            if hr and (header_at is None or "link" in hr.values()):
                roles, header_at = hr, i
                named_metric_cols = {j: r.split(":", 1)[1] for j, r in hr.items()
                                     if r.startswith("metric:")}
                # a header cell can ALSO carry the date ("Date- 4-7-26 Sr No.")
                for c in cells:
                    d = parse_date(c, default_year)
                    if d:
                        date = d
                        if section is None or parse_date(section, default_year):
                            section = re.sub(r"(?i)\bsr\.?\s*no\.?", "", c).strip(" -:") or c
                        break
                continue
            # a heading: section and/or date
            text = " ".join(c for c in cells if c and not _is_number(c)).strip()
            if not text or _norm(text) in _IGNORE_TEXT:
                continue
            d = parse_date(text, default_year)
            if d:
                date = d
                # "Date- 4-7-26" is a date block, not a topic; keep the topic
                # section if one is live, else the date names the block
                if section is None or parse_date(section, default_year):
                    section = text
                continue
            if _has_letters(text):
                section = text
            continue

        # rows with links → posts
        for j, u in row_urls:
            account, metrics = "", {}
            for k, c in enumerate(cells):
                if k == j or not c or urls_in(c):
                    continue
                role = roles.get(k, "")
                if role == "account":
                    account = c
                elif role == "section":
                    section = c or section
                elif role == "date":
                    dd = parse_date(c, default_year)
                    if dd:
                        date = dd
                elif role.startswith("metric:"):
                    if _is_number(c):
                        metrics[role.split(":", 1)[1]] = c
                elif _is_number(c):
                    if k in named_metric_cols:
                        metrics[named_metric_cols[k]] = c
                    else:
                        unnamed_numbers += 1
                elif _has_letters(c) and len(c) <= 60 and not account \
                        and _norm(c) not in _IGNORE_TEXT:
                    dd = parse_date(c, default_year)
                    if dd and not parse_date(section or "", default_year):
                        date = dd
                    else:
                        account = c
            posts.append({"section": section or "", "date": date.isoformat() if date else "",
                          "link": u, "account": account, "metrics": metrics})

    metric_names = []
    for p in posts:
        for k in p["metrics"]:
            if k not in metric_names:
                metric_names.append(k)
    header = ["Section", "Handle", "Link"] + metric_names
    out_grid = [header]
    for p in posts:
        out_grid.append([p["section"], p["account"], p["link"]] +
                        [p["metrics"].get(k, "") for k in metric_names])
    dates = sorted({p["date"] for p in posts if p["date"]})
    sections = []
    for p in posts:
        if p["section"] and p["section"] not in sections:
            sections.append(p["section"])
    shape = ("headered" if header_at is not None else
             "date-blocks" if len(dates) > 1 else
             "sectioned" if len(sections) > 1 else "plain")
    if unnamed_numbers:
        notes.append(f"{unnamed_numbers} number cell(s) sit beside links with no "
                     "column heading — name that column (e.g. Impressions) to print them.")
    if not posts:
        notes.append("No post links found in this tab.")
    return {"posts": posts, "grid": out_grid, "sections": sections, "dates": dates,
            "latest_date": dates[-1] if dates else None,
            "metric_names": metric_names, "unnamed_numbers": unnamed_numbers,
            "shape": shape, "notes": notes, "header_row": header_at}


def latest_block(understood: dict) -> dict:
    """Only the posts of the newest date block (for a tab that stacks dates)."""
    ld = understood.get("latest_date")
    if not ld:
        return understood
    posts = [p for p in understood["posts"] if p["date"] == ld]
    sub = dict(understood)
    sub["posts"] = posts
    sub["grid"] = [understood["grid"][0]] + [
        [p["section"], p["account"], p["link"]] +
        [p["metrics"].get(k, "") for k in understood["metric_names"]] for p in posts]
    return sub


# --------------------------------------------------------------------------- #
# Tabs of a workbook (public htmlview page — no API)
# --------------------------------------------------------------------------- #
_ITEMS = re.compile(r'items\.push\(\{name:\s*"((?:[^"\\]|\\.)*)"[\s\S]{0,400}?gid:\s*"(\d+)"')


def doc_id(url: str) -> str:
    return sheets.describe(url)["doc_id"]


def _unescape(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape").replace("\\/", "/")


def list_tabs(url: str) -> list:
    """[{name, gid, date}] for every tab of the workbook, in sheet order.

    Read from the workbook's htmlview page, which any link-shared sheet serves
    without a login. Falls back to just the tab in the URL if the page cannot
    be read (a workbook shared as one published tab, say)."""
    info = sheets.describe(url)
    tabs = []
    try:
        html = sheets.fetch_text(
            f"https://docs.google.com/spreadsheets/d/{_full_doc_id(url)}/htmlview",
            allow_html=True)
        for m in _ITEMS.finditer(html):
            name = _unescape(m.group(1)).strip()
            tabs.append({"name": name, "gid": m.group(2),
                         "date": (parse_date(name) or None)})
    except sheets.SheetError:
        tabs = []
    if not tabs:
        tabs = [{"name": f"tab {info.get('gid') or 0}", "gid": info.get("gid") or "0",
                 "date": None}]
    for t in tabs:
        t["date"] = t["date"].isoformat() if t["date"] else None
    return tabs


def _full_doc_id(url: str) -> str:
    m = sheets._DOC_ID.search(sheets.urlparse(url).path)
    if not m:
        raise sheets.SheetError("That does not look like a Google Sheets link.")
    return m.group(1)


def tab_url(url: str, gid: str) -> str:
    """The same workbook, pointed at one tab."""
    return f"https://docs.google.com/spreadsheets/d/{_full_doc_id(url)}/edit?gid={gid}#gid={gid}"


def newest_date_tab(tabs: list):
    dated = [t for t in tabs if t.get("date")]
    if not dated:
        return None
    return max(dated, key=lambda t: t["date"])


# --------------------------------------------------------------------------- #
# One call that does the whole thing for a URL
# --------------------------------------------------------------------------- #
def read(url: str, mode: str = "latest", gid: str = None) -> dict:
    """Fetch + understand.

    mode: 'latest' — the newest date tab (or, inside a tab that stacks dates,
                     the newest block); falls back to the URL's tab
          'tab'    — exactly `gid` (or the URL's tab), every date in it
          'all'    — every tab that holds links, concatenated
    Returns understand()'s dict plus {tabs, tab, mode, source_label}.
    """
    tabs = list_tabs(url)
    info = sheets.describe(url)
    chosen = None
    if mode == "all":
        merged = {"posts": [], "grid": None, "notes": [], "metric_names": []}
        used = []
        for t in tabs:
            try:
                u = understand(uploads_grid(url, t["gid"]))
            except sheets.SheetError as e:
                merged["notes"].append(f"{t['name']}: {e}")
                continue
            if not u["posts"]:
                continue
            for p in u["posts"]:
                if not p["section"]:
                    p["section"] = t["name"]
            merged["posts"].extend(u["posts"])
            used.append(t["name"])
        u = _rebuild(merged["posts"])
        u["notes"] = merged["notes"] + u["notes"]
        u.update({"tabs": tabs, "tab": None, "mode": mode,
                  "source_label": f"Google Sheet · {len(used)} tab(s)"})
        return u
    if mode == "tab" and gid:
        chosen = next((t for t in tabs if t["gid"] == str(gid)), None) or \
            {"name": f"tab {gid}", "gid": str(gid), "date": None}
    elif mode == "latest":
        chosen = newest_date_tab(tabs)
    if chosen is None:
        g = info.get("gid") or "0"
        chosen = next((t for t in tabs if t["gid"] == g), None) or \
            {"name": f"tab {g}", "gid": g, "date": None}
    u = understand(uploads_grid(url, chosen["gid"]))
    if mode == "latest" and not chosen.get("date") and len(u["dates"]) > 1:
        u = latest_block(u)
        u["notes"].insert(0, f"Newest date block in this tab: {u['latest_date']}")
    u.update({"tabs": tabs, "tab": chosen, "mode": mode,
              "source_label": f"Google Sheet · {chosen['name']}"})
    return u


def _rebuild(posts: list) -> dict:
    metric_names = []
    for p in posts:
        for k in p["metrics"]:
            if k not in metric_names:
                metric_names.append(k)
    grid = [["Section", "Handle", "Link"] + metric_names] + [
        [p["section"], p["account"], p["link"]] + [p["metrics"].get(k, "") for k in metric_names]
        for p in posts]
    dates = sorted({p["date"] for p in posts if p["date"]})
    sections = []
    for p in posts:
        if p["section"] and p["section"] not in sections:
            sections.append(p["section"])
    return {"posts": posts, "grid": grid, "sections": sections, "dates": dates,
            "latest_date": dates[-1] if dates else None, "metric_names": metric_names,
            "unnamed_numbers": 0, "shape": "merged", "notes": [], "header_row": None}


def uploads_grid(url: str, gid: str) -> list:
    """The raw grid of one tab, via the existing CSV path (same guards)."""
    from . import uploads
    csv_text = sheets.fetch_csv(tab_url(url, gid))
    return uploads.grid_from_csv_text(csv_text)


def fingerprint(understood: dict) -> str:
    """Stable hash of WHAT would be captured — links + sections + dates —
    so a re-fetch that changed nothing costs nothing and starts nothing."""
    import hashlib
    h = hashlib.sha1()
    for p in understood.get("posts") or []:
        h.update(f"{p['date']}|{p['section']}|{p['link']}\n".encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Optional: ask Grok to name columns the heuristics could not
# --------------------------------------------------------------------------- #
def grok_available() -> bool:
    return bool(getattr(config, "XAI_API_KEY", ""))


def grok_label_columns(sample_rows: list) -> dict:
    """{col_index: 'Impressions'|'Views'|'Like'|...|'account'|'section'|'ignore'}

    Sends up to 12 rows of the sheet (links redacted to their host) and asks
    for one JSON object. Any failure returns {} — the heuristics' answer stands.
    """
    if not grok_available():
        return {}
    redacted = [[(re.sub(r"https?://[^ ]+", "<link>", c) if c else "") for c in row]
                for row in sample_rows[:12]]
    prompt = ("These are rows from a social-media report sheet. For each column index, "
              "answer with one label from: link, account, section, date, Like, Impressions, "
              "Views, Reach, Comments, Shares, Followers, ignore. Reply with ONLY a JSON "
              "object mapping column index (string) to label.\n\n" + json.dumps(redacted))
    body = json.dumps({"model": getattr(config, "XAI_MODEL", "grok-4"),
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {config.XAI_API_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        text = text[text.index("{"): text.rindex("}") + 1]
        out = json.loads(text)
        return {int(k): str(v) for k, v in out.items() if str(k).isdigit()}
    except Exception as e:                        # rule 17: say so, keep going
        print(f"[smartsheet] grok column labels unavailable: {e}", flush=True)
        return {}

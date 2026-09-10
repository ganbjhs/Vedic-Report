"""The one bridge between the internal tool and the Client Portal.

Three jobs, all of them WRITES into `data/portal.db` (the portal only reads):

* `publish_run(job_id)` — after a run finishes: every captured post of a
  project linked to a client becomes a `post_metrics` row for that client,
  visible from `sheet_date + client.lag_days`. Called from
  `webapp/jobs/runner.py` when a job is marked done; never fails the run.
* `sync_client(client_id, day_from, day_to)` — ask the client's scraper for
  those days and fold the answer in: content (caption, media, avatar, times)
  and fresh counts for posts the sheet listed, plus any post the scraper
  knows that the sheet did not. Runs from Admin → Clients ("Sync now") and
  from the scheduler below.
* `backfill_client(client_id)` — every finished run of the client's projects,
  through `publish_run`. What `scripts/portal_backfill.py` calls.

Imports from the portal package are limited to its leaf modules
(`schema`, `util`, `secretbox`, `scraper`) — standard library only, so the
internal app gains no dependency and the portal gains no reverse dependency.
"""
import datetime as _dt
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from portal import schema as pschema
from portal import scraper as pscraper
from portal import secretbox
from portal import util as putil

from . import config
from .jobs import store

PORTAL_DB = Path(getattr(config, "PORTAL_DB", "")) if getattr(config, "PORTAL_DB", "") else (config.DATA_DIR / "portal.db")
MEDIA_DIR = Path(getattr(config, "PORTAL_MEDIA_DIR", "")) if getattr(config, "PORTAL_MEDIA_DIR", "") else (config.DATA_DIR / "portal" / "media")
KEY_SECRET = getattr(config, "PORTAL_KEY_SECRET", "") or ""
SYNC_MINUTES = int(getattr(config, "PORTAL_SYNC_MINUTES", 60) or 60)
SYNC_DAYS_BACK = int(getattr(config, "PORTAL_SYNC_DAYS_BACK", 4) or 4)
SYNC_AT = str(getattr(config, "PORTAL_SYNC_AT", "") or "").strip()
SYNC_TZ = str(getattr(config, "PORTAL_TZ", "") or "Asia/Kolkata")

_lock = threading.Lock()


def connect():
    pschema.ensure_schema(PORTAL_DB)
    return pschema.connect(PORTAL_DB)


# --------------------------------------------------------------------------- #
# Clients (read side used by the admin page and the hooks)
# --------------------------------------------------------------------------- #
def clients_for_project(conn, project_id: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT c.* FROM clients c JOIN client_projects cp ON cp.client_id = c.id "
        "WHERE cp.project_id = ? AND c.archived = 0", (project_id,)).fetchall()]


def client_get(conn, cid: str):
    r = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
    return dict(r) if r else None


def _log(conn, client_id: str, kind: str, posts: int, run_id: str = "", sheet_date: str = "",
         visible_from: str = "", note: str = "", by_user: str = "") -> None:
    conn.execute("INSERT INTO publish_log (client_id, run_id, kind, sheet_date, posts, visible_from, note, by_user, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (client_id, run_id, kind, sheet_date, posts, visible_from, (note or "")[:400], by_user, time.time()))


# --------------------------------------------------------------------------- #
# Metrics out of a results.json row
# --------------------------------------------------------------------------- #
_SHEET_KEYS = {
    "likes": ("like", "likes", "reactions", "reaction"),
    "comments": ("comments", "comment", "replies"),
    "shares": ("shares", "share", "reposts", "retweets", "rt"),
    "views": ("views", "view", "video views", "plays"),
    "reach": ("reach", "post reach", "reach/views"),
    "impressions": ("impressions", "impression", "post impression", "post impressions"),
}


def _metrics_of(row: dict, read: dict) -> tuple:
    """(dict of ints, source). Sheet columns first (a typed number wins),
    then the numbers read off the screenshot / the page."""
    out = {k: None for k in _SHEET_KEYS}
    src = set()
    sheet = {str(k).strip().lower(): v for k, v in (row.get("sheet_metrics") or {}).items()}
    for key, names in _SHEET_KEYS.items():
        for n in names:
            if n in sheet and putil.to_int(sheet[n]) is not None:
                out[key] = putil.to_int(sheet[n])
                src.add("sheet")
                break
    for key in ("likes", "comments", "shares", "views"):
        if out[key] is None and read and putil.to_int(read.get(key)) is not None:
            out[key] = putil.to_int(read.get(key))
            src.add("ocr" if (read.get("engine") or "") != "page" else "page")
    inf = row.get("metrics") or {}                      # influencer engine
    for key, names in (("likes", ("reactions", "likes")), ("comments", ("comments",)),
                       ("shares", ("shares",)), ("reach", ("reach",))):
        if out[key] is None:
            for n in names:
                if putil.to_int(inf.get(n)) is not None:
                    out[key] = putil.to_int(inf.get(n))
                    src.add("page")
                    break
    if out["views"] is None and out["reach"] is not None and row.get("platform") == "x":
        pass                                            # X 'views' stays None unless read; never copy
    source = "mixed" if len(src) > 1 else (next(iter(src)) if src else "none")
    return out, source


def _read_by_shot(app: Path) -> dict:
    f = app / "reports" / "metrics_read.json"
    try:
        rows = json.loads(f.read_text(encoding="utf-8"))
        return {str(r.get("screenshot") or ""): r for r in rows if isinstance(r, dict)}
    except (OSError, ValueError):
        return {}


def _sheet_date_of(job: dict) -> _dt.date:
    d = putil.parse_day(job.get("sheet_date") or "")
    if d:
        return d
    created = job.get("created_at") or time.time()
    return _dt.datetime.fromtimestamp(created).date()


# --------------------------------------------------------------------------- #
# publish_run
# --------------------------------------------------------------------------- #
def publish_run(job_id: str, by_user: str = "auto", quiet: bool = False) -> dict:
    """Publish one finished job to every client its project is linked to.
    Returns {clients: n, posts: n}. Never raises for a job that is simply not
    a client job — that is the common case."""
    from .jobs import runner
    job = store.get(job_id)
    if not job or not job.get("project_id"):
        return {"clients": 0, "posts": 0}
    with _lock:
        conn = connect()
        try:
            clients = clients_for_project(conn, job["project_id"])
            if not clients:
                return {"clients": 0, "posts": 0}
            app = runner.app_dir(job_id)
            results = runner._read_results(app)
            if not results:
                return {"clients": len(clients), "posts": 0}
            read = _read_by_shot(app)
            sheet_date = _sheet_date_of(job)
            total = 0
            for c in clients:
                n = _publish_rows(conn, c, job, results, read, sheet_date)
                _publish_report_files(conn, c, job, sheet_date)
                total += n
                vis = putil.day_str(putil.add_days(sheet_date, int(c.get("lag_days") or 2)))
                _log(conn, c["id"], "run", n, run_id=job_id, sheet_date=putil.day_str(sheet_date),
                     visible_from=vis, note=f"{job.get('title') or job_id}", by_user=by_user)
                if not quiet:
                    store.append_activity(job_id, f"Published {n} post(s) to {c['name']} "
                                                  f"(visible from {vis}).")
            conn.commit()
            return {"clients": len(clients), "posts": total}
        finally:
            conn.close()


def _publish_rows(conn, client: dict, job: dict, results: list, read: dict, sheet_date) -> int:
    lag = int(client.get("lag_days") or 2)
    day = putil.day_str(sheet_date)
    visible_from = putil.day_str(putil.add_days(sheet_date, lag))
    now = time.time()
    n = 0
    for r in results:
        url = (r.get("post_link") or r.get("url") or "").strip()
        if not url:
            continue
        norm = putil.norm_url(url)
        plat = putil.platform_of(url, r.get("platform") or "")
        ok = (r.get("status") == "ok") and bool(r.get("screenshot"))
        metrics, source = _metrics_of(r, read.get(str(r.get("screenshot") or "")) or {})
        shot_rel = ""
        if ok and client.get("show_screenshots") and r.get("screenshot"):
            shot_rel = _copy_shot(client, job["id"], r["screenshot"])
        handle = (r.get("handle") or "").strip()
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        display = (r.get("display_name") or r.get("account_name") or "").strip()
        existing = conn.execute("SELECT id, first_published_at, caption, metric_source FROM post_metrics "
                                "WHERE client_id = ? AND post_url_norm = ? AND sheet_date = ?",
                                (client["id"], norm, day)).fetchone()
        raw = json.dumps({"sheet_metrics": r.get("sheet_metrics") or {}, "metrics": r.get("metrics") or {},
                          "read": {k: v for k, v in (read.get(str(r.get("screenshot") or "")) or {}).items()
                                   if k in ("likes", "comments", "shares", "views", "shown", "engine")}},
                         ensure_ascii=False)
        if existing:
            # a later run refreshes counts + status; scraper content and counts
            # are fresher than OCR, so a scraper-sourced row keeps its numbers
            keep_scraper = existing["metric_source"] == "scraper"
            sets = ["project_id = ?", "run_id = ?", "visible_from = ?", "captured_at = ?", "platform = ?",
                    "category_raw = ?", "status = ?", "skip_reason = ?", "published_at = ?", "raw_metrics = ?"]
            vals = [job.get("project_id") or "", job["id"], visible_from, job.get("finished_at") or now, plat,
                    r.get("category") or "", "ok" if ok else "skipped", "" if ok else (r.get("status") or "not captured"),
                    now, raw]
            if handle:
                sets.append("handle = ?"); vals.append(handle)
            if display:
                sets.append("display_name = ?"); vals.append(display)
            if shot_rel:
                sets.append("screenshot_path = ?"); vals.append(shot_rel)
            if not keep_scraper:
                for k in ("likes", "comments", "shares", "views", "reach", "impressions"):
                    sets.append(f"{k} = ?"); vals.append(metrics[k])
                sets.append("metric_source = ?"); vals.append(source)
            vals.append(existing["id"])
            conn.execute(f"UPDATE post_metrics SET {', '.join(sets)} WHERE id = ?", vals)
        else:
            conn.execute(
                "INSERT INTO post_metrics (client_id, project_id, run_id, sheet_date, visible_from, captured_at, "
                "platform, category_raw, handle, display_name, post_url, post_url_norm, post_type, likes, comments, "
                "shares, views, reach, impressions, metric_source, raw_metrics, status, skip_reason, screenshot_path, "
                "first_published_at, published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client["id"], job.get("project_id") or "", job["id"], day, visible_from,
                 job.get("finished_at") or now, plat, r.get("category") or "", handle, display, url, norm, "",
                 metrics["likes"], metrics["comments"], metrics["shares"], metrics["views"], metrics["reach"],
                 metrics["impressions"], source, raw, "ok" if ok else "skipped",
                 "" if ok else (r.get("status") or "not captured"), shot_rel, now, now))
        _snapshot(conn, client, post_key=norm, day=putil.day_str(putil.today_in(
                      client.get("tz") or "Asia/Kolkata")),
                  sheet_date=day, counts=metrics, post_url_norm=norm, platform=plat,
                  category_raw=r.get("category") or "", status="ok" if ok else "skipped",
                  metric_source=source, now=now)
        n += 1
    return n


_REPORT_FMTS = ("pdf", "pptx", "docx")


def _publish_report_files(conn, client: dict, job: dict, sheet_date) -> int:
    """Copy a finished run's report files (pdf/pptx/docx) into the client's
    portal media folder and record them, so the read-only portal can offer
    them for download on the report day. Screenshot bundles are skipped."""
    from .jobs import runner
    try:
        out = runner.out_dir(job["id"])
    except Exception:
        return 0
    if not out.is_dir():
        return 0
    lag = int(client.get("lag_days") or 2)
    day = putil.day_str(sheet_date)
    visible_from = putil.day_str(putil.add_days(sheet_date, lag))
    now = time.time()
    n = 0
    for src in sorted(out.iterdir()):
        if not src.is_file() or src.name.endswith("_screenshots.zip"):
            continue
        ext = src.suffix.lower().lstrip(".")
        if ext not in _REPORT_FMTS:
            continue
        rel = f"{client['slug']}/reports/{job['id']}/{src.name}"
        dest = MEDIA_DIR / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists() or dest.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, dest)
        except OSError as e:
            print(f"[portal] report not copied ({e})", flush=True)
            continue
        conn.execute(
            "INSERT INTO client_reports (id, client_id, project_id, run_id, sheet_date, visible_from, fmt, "
            "label, rel_path, bytes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id, run_id, fmt) DO UPDATE SET rel_path=excluded.rel_path, "
            "bytes=excluded.bytes, sheet_date=excluded.sheet_date, visible_from=excluded.visible_from",
            (uuid.uuid4().hex[:12], client["id"], job.get("project_id") or "", job["id"], day, visible_from,
             ext, src.name, rel, src.stat().st_size, now))
        n += 1
    return n


def _copy_shot(client: dict, job_id: str, shot: str) -> str:
    try:
        src = Path(shot)
        if not src.is_file():
            return ""
        rel = Path(job_id) / src.name
        dest = MEDIA_DIR / client["slug"] / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(src, dest)
        return str(rel)
    except OSError as e:
        print(f"[portal] screenshot not copied ({e})", flush=True)
        return ""


# --------------------------------------------------------------------------- #
# The scraper
# --------------------------------------------------------------------------- #
def sync_client(client_id: str, day_from: str = "", day_to: str = "", by_user: str = "auto",
                source_id: str = "") -> dict:
    """Fetch the client's scraper for [day_from, day_to] and fold it into
    post_metrics. Returns {sources: n, posts: n, matched: n, added: n, errors: [..]}."""
    if not KEY_SECRET:
        return {"sources": 0, "posts": 0, "matched": 0, "added": 0,
                "errors": ["PORTAL_KEY_SECRET is not set, so the saved API key cannot be opened."]}
    with _lock:
        conn = connect()
        try:
            client = client_get(conn, client_id)
            if not client:
                raise KeyError(client_id)
            lag = int(client.get("lag_days") or 2)
            today = putil.today_in(client.get("tz") or "Asia/Kolkata")
            b = putil.parse_day(day_to) or today
            a = putil.parse_day(day_from) or putil.add_days(b, -(SYNC_DAYS_BACK - 1))
            q = "SELECT * FROM client_sources WHERE client_id = ? AND enabled = 1"
            args = [client_id]
            if source_id:
                q += " AND id = ?"
                args.append(source_id)
            sources = [dict(r) for r in conn.execute(q, args).fetchall()]
            out = {"sources": len(sources), "posts": 0, "matched": 0, "added": 0, "days": 0,
                   "skipped": [], "errors": []}
            for s in sources:
                try:
                    key = secretbox.open_(KEY_SECRET, s.get("api_key_enc") or "") if s.get("api_key_enc") else ""
                except ValueError as e:
                    out["errors"].append(f"{s['signature_name']}: {e}")
                    conn.execute("UPDATE client_sources SET last_error = ? WHERE id = ?", (str(e)[:400], s["id"]))
                    continue
                if s.get("probe_url"):
                    # The Collector says when it is part-way through re-reading
                    # every link. A snapshot taken then is a half-scraped day
                    # recorded for ever, so skip and take it tomorrow.
                    try:
                        info = pscraper.probe(s, key)
                    except pscraper.ScraperError as e:
                        out["errors"].append(f"{s['signature_name']}: handshake failed — {e}")
                        conn.execute("UPDATE client_sources SET last_error = ? WHERE id = ?",
                                     (str(e)[:400], s["id"]))
                        continue
                    if info.get("refresh_in_progress"):
                        out["skipped"].append(f"{s['signature_name']}: mid-refresh, left for the next run")
                        _log(conn, client_id, "scraper", 0, note=f"{s['signature_name']}: skipped, mid-refresh",
                             by_user=by_user)
                        continue
                try:
                    posts, body = pscraper.fetch(s, key, putil.day_str(a), putil.day_str(b))
                except pscraper.ScraperError as e:
                    out["errors"].append(f"{s['signature_name']}: {e}")
                    conn.execute("UPDATE client_sources SET last_error = ? WHERE id = ?", (str(e)[:400], s["id"]))
                    continue
                matched, added, snapped = _fold_scraper(conn, client, s, posts, lag, a, b,
                                                        pull_day=putil.day_str(today))
                out["posts"] += len(posts)
                out["matched"] += matched
                out["added"] += added
                out["days"] = out.get("days", 0) + snapped
                conn.execute("UPDATE client_sources SET last_ok_at = ?, last_error = '', last_count = ? WHERE id = ?",
                             (time.time(), len(posts), s["id"]))
                # cache the answer per day it covered
                by_day = {}
                for p in posts:
                    by_day.setdefault(p["day"] or putil.day_str(b), []).append(p["raw"])
                for d, items in by_day.items():
                    conn.execute("INSERT OR REPLACE INTO scraper_cache (client_id, source_id, day, fetched_at, post_count, body) "
                                 "VALUES (?, ?, ?, ?, ?, ?)",
                                 (client_id, s["id"], d, time.time(), len(items), json.dumps(items, ensure_ascii=False)))
                _log(conn, client_id, "scraper", len(posts), sheet_date=f"{putil.day_str(a)}..{putil.day_str(b)}",
                     note=f"{s['signature_name']}: {matched} matched, {added} added, "
                          f"{snapped} snapshot(s) for {putil.day_str(today)}", by_user=by_user)
            conn.commit()
            return out
        finally:
            conn.close()


_SKIP_NOTE = {"removed": "removed from the sheet",
              "unavailable": "no longer available on the platform"}

# Which measurement wins when the operator typed a number into the sheet AND
# the Collector scraped one. For X we read the count off X itself, so the
# scraped number is the measurement. For everything else the Collector cannot
# fetch per-post counters yet, so the typed number is the ONLY measurement
# there is. The loser is never discarded — it is kept under `raw_metrics` so a
# typo can be caught by comparing the two rather than by noticing a chart looks
# wrong. See REPORT_TOOL_PLAN.md §5.3.
_SCRAPER_WINS = ("x",)

_COUNTS = ("likes", "comments", "shares", "views", "reach",
           "quotes", "bookmarks", "author_followers")
_CONTENT = ("display_name", "handle", "avatar_url", "caption", "lang",
            "media_type", "thumb_url", "posted_at", "collected_at")


_SNAP_COUNTS = ("likes", "comments", "shares", "views", "quotes", "bookmarks",
                "reach", "impressions", "author_followers")


def _snapshot(conn, client: dict, *, post_key: str, day: str, sheet_date: str,
              counts: dict, tweet_id: str = "", post_url_norm: str = "",
              platform: str = "", category_raw: str = "", status: str = "ok",
              metric_source: str = "scraper", last_refresh_ms=None,
              refresh_count=None, now: float = 0.0) -> int:
    """One row per post per PULL day, in `post_metric_days`. Returns 1 if a row
    was written, 0 if there was nothing worth recording.

    This is the only history that exists. The Collector overwrites its counters
    in place, the sheet is retyped over itself, and `post_metrics` is current
    state — so a number not written here is gone the moment it changes.

    Every writer of `post_metrics` calls this, not just the scraper. The sheet
    is where Facebook, Instagram and YouTube numbers come from, and for those
    posts it is the ONLY source there will be until the Collector can fetch
    them; without this their charts could never move.
    """
    if not post_key or not day:
        return 0
    vals = {k: counts.get(k) for k in _SNAP_COUNTS}
    if all(v is None for v in vals.values()) and status == "ok":
        return 0                       # nothing measured — do not imply we looked
    conn.execute(
        "INSERT OR REPLACE INTO post_metric_days (client_id, post_key, day, tweet_id, post_url_norm, "
        "sheet_date, platform, category_raw, likes, comments, shares, views, quotes, bookmarks, "
        "reach, impressions, author_followers, status, last_refresh_ms, refresh_count, "
        "metric_source, pulled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (client["id"], post_key, day, tweet_id, post_url_norm, sheet_date, platform,
         category_raw, vals["likes"], vals["comments"], vals["shares"], vals["views"],
         vals["quotes"], vals["bookmarks"], vals["reach"], vals["impressions"],
         vals["author_followers"], status, last_refresh_ms, refresh_count,
         metric_source, now or time.time()))
    return 1


def _fold_scraper(conn, client: dict, source: dict, posts: list, lag: int, a, b,
                  pull_day: str = "") -> tuple:
    """Fold one scraper answer into `post_metrics`, and record the day.

    Returns (matched, added, snapshots).

    Three rules this function exists to keep:

    * **Exactly one `post_metrics` row is touched per post.** It used to update
      every row carrying that URL, across every `sheet_date` — so a daily pull
      rewrote yesterday's numbers with today's and the growth charts flattened
      to nothing. History now lives in `post_metric_days`; the current-state row
      is the post's own day and nothing else.
    * **`status` is obeyed.** A post X has deleted, or one the operator took out
      of the sheet, is marked `skipped` and disappears from the client's
      dashboard instead of showing stale numbers for ever.
    * **A platform we do not model is skipped, never guessed at.**
      `util.platform_of()` ends `return "x"`, so a YouTube link would otherwise
      be charted as X.
    """
    matched = added = snapped = 0
    now = time.time()
    tz = client.get("tz") or "Asia/Kolkata"
    pull_day = pull_day or putil.day_str(putil.today_in(tz))
    sig = source.get("signature_name") or "scraper"
    lo, hi = putil.day_str(a), putil.day_str(b)

    for p in posts:
        if not p.get("post_url_norm"):
            continue
        if p.get("platform") not in putil.PLATFORMS:
            continue
        pid = (p.get("post_id") or "").strip()
        status = (p.get("status") or "ok").lower()
        row_status = "skipped" if status in _SKIP_NOTE else "ok"
        skip_reason = "" if row_status == "ok" else (p.get("status_note") or _SKIP_NOTE[status])

        if pid:
            rows = conn.execute(
                "SELECT id, sheet_date, metric_source, raw_metrics FROM post_metrics "
                "WHERE client_id = ? AND (tweet_id = ? OR post_url_norm = ?) ORDER BY sheet_date DESC",
                (client["id"], pid, p["post_url_norm"])).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, sheet_date, metric_source, raw_metrics FROM post_metrics "
                "WHERE client_id = ? AND post_url_norm = ? ORDER BY sheet_date DESC",
                (client["id"], p["post_url_norm"])).fetchall()

        # The day this post is filed under: the Collector's `day` (the sheet
        # tab's own date) when it sent one, else the day of the row we already
        # have, else the day we pulled.
        day = p.get("day") or (rows[0]["sheet_date"] if rows else pull_day)

        # Exactly one row: the one for this day, else the one that exists.
        target = next((r for r in rows if r["sheet_date"] == day), None) or (rows[0] if rows else None)

        # Whether the scraped counts may overwrite what is stored.
        prev_source = (target["metric_source"] if target else "") or ""
        write_counts = p["platform"] in _SCRAPER_WINS or prev_source != "sheet"
        scraped = {k: p.get(k) for k in _COUNTS}

        if target:
            sets, vals = [], []
            for k in _CONTENT:
                if p.get(k):
                    sets.append(f"{k} = ?"); vals.append(p[k])
            if pid:
                sets.append("tweet_id = ?"); vals.append(pid)
            if write_counts:
                for k in _COUNTS:
                    sets.append(f"{k} = ?"); vals.append(scraped[k])
                sets.append("metric_source = ?"); vals.append("scraper")
            try:
                raw = json.loads(target["raw_metrics"] or "{}")
            except ValueError:
                raw = {}
            raw["scraper"] = dict(scraped, signature=sig, last_refresh_ms=p.get("last_refresh_ms"),
                                  applied=bool(write_counts))
            sets += ["status = ?", "skip_reason = ?", "raw_metrics = ?", "last_refresh_ms = ?",
                     "refresh_count = ?", "published_at = ?"]
            vals += [row_status, skip_reason, json.dumps(raw, ensure_ascii=False),
                     p.get("last_refresh_ms"), p.get("refresh_count"), now, target["id"]]
            conn.execute(f"UPDATE post_metrics SET {', '.join(sets)} WHERE id = ?", vals)
            matched += 1
        else:
            # A post the sheet never listed. When the Collector told us the day
            # it is trusted outright; when the day was merely guessed from the
            # post's own publish time, the old sync window still applies, so a
            # legacy scraper cannot backfill years of rows by accident.
            if not p.get("day_given") and (day < lo or day > hi):
                continue
            d = putil.parse_day(day)
            if not d:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO post_metrics (client_id, project_id, run_id, sheet_date, visible_from, "
                "captured_at, platform, category_raw, handle, display_name, avatar_url, post_url, post_url_norm, "
                "tweet_id, post_type, caption, lang, media_type, thumb_url, posted_at, collected_at, likes, comments, "
                "shares, views, reach, impressions, quotes, bookmarks, author_followers, last_refresh_ms, "
                "refresh_count, metric_source, raw_metrics, status, skip_reason, screenshot_path, "
                "first_published_at, published_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client["id"], "", "", day, putil.day_str(putil.add_days(d, lag)), now, p["platform"],
                 p.get("category_raw") or "", p["handle"], p["display_name"], p["avatar_url"], p["post_url"],
                 p["post_url_norm"], pid, "", p["caption"], p.get("lang") or "", p["media_type"], p["thumb_url"],
                 p["posted_at"], p["collected_at"], p["likes"], p["comments"], p["shares"], p["views"],
                 p["reach"], None, p.get("quotes"), p.get("bookmarks"), p.get("author_followers"),
                 p.get("last_refresh_ms"), p.get("refresh_count"), "scraper",
                 json.dumps({"scraper": dict(scraped, signature=sig)}, ensure_ascii=False),
                 row_status, skip_reason, "", now, now))
            added += 1

        snapped += _snapshot(
            conn, client, post_key=(pid or p["post_url_norm"]), day=pull_day, sheet_date=day,
            counts={k: p.get(k) for k in _SNAP_COUNTS}, tweet_id=pid,
            post_url_norm=p["post_url_norm"], platform=p["platform"],
            category_raw=p.get("category_raw") or "", status=status or "ok",
            metric_source="scraper", last_refresh_ms=p.get("last_refresh_ms"),
            refresh_count=p.get("refresh_count"), now=now)
    return matched, added, snapped


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #
def backfill_client(client_id: str, by_user: str = "backfill") -> dict:
    conn = connect()
    try:
        client = client_get(conn, client_id)
        if not client:
            raise KeyError(client_id)
        pids = [r["project_id"] for r in conn.execute(
            "SELECT project_id FROM client_projects WHERE client_id = ?", (client_id,)).fetchall()]
    finally:
        conn.close()
    jobs = 0
    posts = 0
    for pid in pids:
        for j in store.list_for_project(pid, limit=5000):
            if j.get("status") != "done":
                continue
            r = publish_run(j["id"], by_user=by_user, quiet=True)
            jobs += 1
            posts += r.get("posts", 0)
    conn = connect()
    try:
        _log(conn, client_id, "backfill", posts, note=f"{jobs} run(s)", by_user=by_user)
        conn.commit()
    finally:
        conn.close()
    return {"runs": jobs, "posts": posts}


# --------------------------------------------------------------------------- #
# Sheet -> dashboard (no capture): read the client's Google Sheet directly
# --------------------------------------------------------------------------- #
_SHEET_METRIC_KEYS = ("likes", "comments", "shares", "views", "reach", "impressions")


def _publish_sheet_rows(conn, client: dict, src: dict, rows: list, sheet_date) -> int:
    """Upsert analysed sheet rows straight into post_metrics as VISIBLE posts.
    Unlike a capture run there is no screenshot: a sheet row IS the post, so it
    is 'ok' on its own, and the sheet's typed numbers are authoritative."""
    lag = int(client.get("lag_days") or 2)
    day = putil.day_str(sheet_date)
    visible_from = putil.day_str(putil.add_days(sheet_date, lag))
    pull_day = putil.day_str(putil.today_in(client.get("tz") or "Asia/Kolkata"))
    now = time.time()
    n = 0
    for r in rows:
        url = (r.get("post_link") or r.get("link") or r.get("url") or "").strip()
        if not url:
            continue
        norm = putil.norm_url(url)
        plat = putil.platform_of(url, r.get("platform") or "")
        metrics, _ = _metrics_of(r, {})                     # sheet_metrics only (read={})
        source = "sheet" if any(metrics[k] is not None for k in _SHEET_METRIC_KEYS) else "none"
        handle = (r.get("handle") or "").strip()
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        display = (r.get("display_name") or r.get("account_name") or "").strip()
        category = (r.get("category") or r.get("section") or "").strip()
        raw = json.dumps({"sheet_metrics": r.get("sheet_metrics") or {}}, ensure_ascii=False)
        existing = conn.execute("SELECT id FROM post_metrics WHERE client_id=? AND post_url_norm=? AND sheet_date=?",
                                (client["id"], norm, day)).fetchone()
        if existing:
            sets = ["project_id=?", "run_id=?", "visible_from=?", "platform=?", "category_raw=?",
                    "status='ok'", "skip_reason=''", "published_at=?", "raw_metrics=?", "metric_source=?"]
            vals = [src.get("project_id") or "", "sheet", visible_from, plat, category, now, raw, source]
            for k in _SHEET_METRIC_KEYS:
                sets.append(f"{k}=?"); vals.append(metrics[k])
            if handle:
                sets.append("handle=?"); vals.append(handle)
            if display:
                sets.append("display_name=?"); vals.append(display)
            vals.append(existing["id"])
            conn.execute(f"UPDATE post_metrics SET {', '.join(sets)} WHERE id=?", vals)
        else:
            conn.execute(
                "INSERT INTO post_metrics (client_id, project_id, run_id, sheet_date, visible_from, captured_at, "
                "platform, category_raw, handle, display_name, post_url, post_url_norm, post_type, likes, comments, "
                "shares, views, reach, impressions, metric_source, raw_metrics, status, skip_reason, screenshot_path, "
                "first_published_at, published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client["id"], src.get("project_id") or "", "sheet", day, visible_from, now, plat, category,
                 handle, display, url, norm, "", metrics["likes"], metrics["comments"], metrics["shares"],
                 metrics["views"], metrics["reach"], metrics["impressions"], source, raw, "ok", "", "", now, now))
        # The sheet is the only source Facebook, Instagram and YouTube have.
        # Without this their history is empty and every growth chart is flat.
        _snapshot(conn, client, post_key=norm, day=pull_day, sheet_date=day,
                  counts=metrics, post_url_norm=norm, platform=plat,
                  category_raw=category, metric_source=source, now=now)
        n += 1
    return n


def _dashboard_sheet_sources(conn, client_id: str) -> list:
    """The client's linked-project sheet sources whose purpose feeds the dashboard."""
    pids = [row["project_id"] for row in conn.execute(
        "SELECT project_id FROM client_projects WHERE client_id=?", (client_id,)).fetchall()]
    out = []
    for pid in pids:
        for s in store.sources_for(pid):
            if s.get("enabled") and (s.get("kind") or "sheet") == "sheet" \
               and (s.get("purpose") or "report") in ("dashboard", "both"):
                out.append(s)
    return out


def publish_from_sheet(client_id: str, by_user: str = "auto", max_days: int = 90) -> dict:
    """Read the client's dashboard sheets directly (no capture) and upsert every
    day tab's rows into post_metrics. Day tabs are read by their date; a sheet
    with no dated tabs falls back to its newest date block."""
    from . import smartsheet, uploads                        # heavy; imported on use
    conn = connect()
    try:
        client = client_get(conn, client_id)
        if not client:
            raise KeyError(client_id)
        srcs = _dashboard_sheet_sources(conn, client_id)
        out = {"sources": len(srcs), "days": 0, "posts": 0, "history": 0, "errors": []}
        for s in srcs:
            try:
                tabs = smartsheet.list_tabs(s["url"])
            except Exception as e:                            # rule 17: note it, keep going
                out["errors"].append(f"{s.get('label') or s['url']}: {e}")
                continue
            dated = [t for t in tabs if t.get("date")]
            plan = dated[-max_days:] if dated else [None]     # None -> newest block via mode=latest
            for t in plan:
                try:
                    if t is None:
                        u = smartsheet.read(s["url"], mode="latest", gid=s.get("gid") or None)
                        d = u.get("latest_date")
                    else:
                        u = smartsheet.read(s["url"], mode="tab", gid=t["gid"])
                        d = t.get("date") or u.get("latest_date")
                    sd = putil.parse_day(d) if d else None
                    if not sd:
                        continue
                    rows = uploads.analyse(u["grid"], False, "combined")["rows"]
                    out["posts"] += _publish_sheet_rows(conn, client, s, rows, sd)
                    out["days"] += 1
                except Exception as e:
                    label = t["name"] if t else "latest"
                    out["errors"].append(f"{s.get('label') or s['url']} ({label}): {e}")
            try:
                store.source_update(s["id"], last_checked_at=time.time())
            except Exception:
                pass
        today = putil.day_str(putil.today_in(client.get("tz") or "Asia/Kolkata"))
        out["history"] = conn.execute(
            "SELECT COUNT(*) FROM post_metric_days WHERE client_id = ? AND day = ?",
            (client_id, today)).fetchone()[0]
        _log(conn, client_id, "sheet", out["posts"],
             note=f"{out['days']} day(s), {out['sources']} sheet(s), "
                  f"{out['history']} post(s) with history for {today}", by_user=by_user)
        conn.commit()
        return out
    finally:
        conn.close()


def sync_sheets_all(by_user: str = "auto") -> list:
    conn = connect()
    try:
        ids = [row["id"] for row in conn.execute("SELECT id FROM clients WHERE archived=0").fetchall()]
    finally:
        conn.close()
    out = []
    for cid in ids:
        try:
            r = publish_from_sheet(cid, by_user=by_user)
            if r["sources"]:
                out.append((cid, r))
        except Exception as e:
            print(f"[portal] sheet sync {cid} failed: {e}", flush=True)
    return out


# --------------------------------------------------------------------------- #
# Scheduler — sheet sync (always) + scraper sync (when a key is set)
# --------------------------------------------------------------------------- #
_STOP = threading.Event()
_THREAD = None
_LAST_RUN_DAY = ""


def _interval_due(now_utc: float) -> bool:
    """The old clock: every PORTAL_SYNC_MINUTES, on a fixed minute of the Unix
    epoch. Good enough for re-reading a Google Sheet, which is cheap and which
    an operator edits all day and expects to see land."""
    return int(now_utc // 60) % max(1, SYNC_MINUTES) == 0


def _daily_due(now_utc: float) -> bool:
    """The scraper clock: once a day at PORTAL_SYNC_AT in PORTAL_TZ, e.g. 03:30
    IST — after the local day has closed, which is the only moment a day's
    numbers are final. Unset falls back to the interval.

    This is deliberately SEPARATE from the sheet clock. They were briefly the
    same, which quietly dropped the sheet from hourly to daily: the sheet is a
    local read of a document a human is editing, the scraper walk is 1,600
    remote posts whose counters only settle once. One schedule cannot be right
    for both.
    """
    global _LAST_RUN_DAY
    if not SYNC_AT:
        return _interval_due(now_utc)
    try:
        hh, mm = (int(x) for x in SYNC_AT.split(":", 1))
    except ValueError:
        return _interval_due(now_utc)
    try:
        from zoneinfo import ZoneInfo
        local = _dt.datetime.fromtimestamp(now_utc, ZoneInfo(SYNC_TZ))
    except Exception:
        local = _dt.datetime.fromtimestamp(now_utc)
    today = local.strftime("%Y-%m-%d")
    if today == _LAST_RUN_DAY or (local.hour, local.minute) < (hh, mm):
        return False
    _LAST_RUN_DAY = today            # a restart at 03:31 still catches the day
    return True


def sync_all(by_user: str = "auto") -> list:
    conn = connect()
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT DISTINCT c.id FROM clients c JOIN client_sources s ON s.client_id = c.id "
            "WHERE c.archived = 0 AND s.enabled = 1").fetchall()]
    finally:
        conn.close()
    out = []
    for cid in ids:
        try:
            out.append((cid, sync_client(cid, by_user=by_user)))
        except Exception as e:                  # rule 17: say so, keep going
            print(f"[portal] sync {cid} failed: {e}", flush=True)
    return out


def _loop():
    """Two clocks, checked every minute. The sheet is cheap and a human edits
    it all day, so it stays on the interval; the scraper walk is 1,600 remote
    posts whose counters only settle once the local day has closed, so it runs
    at PORTAL_SYNC_AT. Neither leg may raise: this thread going down takes the
    sync with it silently."""
    while not _STOP.wait(60):
        now = time.time()
        if _interval_due(now):
            try:
                for cid, r in sync_sheets_all():
                    if r.get("errors"):
                        print(f"[portal] sheet sync {cid}: {'; '.join(r['errors'])}", flush=True)
            except Exception as e:
                print(f"[portal] sheet sync loop error: {e}", flush=True)
        if KEY_SECRET and _daily_due(now):
            try:
                for cid, r in sync_all():
                    if r.get("errors"):
                        print(f"[portal] sync {cid}: {'; '.join(r['errors'])}", flush=True)
            except Exception as e:
                print(f"[portal] sync loop error: {e}", flush=True)


def start_scheduler() -> None:
    global _THREAD
    if _THREAD is not None:
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="portal-sync", daemon=True)
    _THREAD.start()


def stop_scheduler() -> None:
    _STOP.set()

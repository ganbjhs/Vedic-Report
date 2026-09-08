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
            out = {"sources": len(sources), "posts": 0, "matched": 0, "added": 0, "errors": []}
            for s in sources:
                try:
                    key = secretbox.open_(KEY_SECRET, s.get("api_key_enc") or "") if s.get("api_key_enc") else ""
                except ValueError as e:
                    out["errors"].append(f"{s['signature_name']}: {e}")
                    conn.execute("UPDATE client_sources SET last_error = ? WHERE id = ?", (str(e)[:400], s["id"]))
                    continue
                try:
                    posts, body = pscraper.fetch(s, key, putil.day_str(a), putil.day_str(b))
                except pscraper.ScraperError as e:
                    out["errors"].append(f"{s['signature_name']}: {e}")
                    conn.execute("UPDATE client_sources SET last_error = ? WHERE id = ?", (str(e)[:400], s["id"]))
                    continue
                matched, added = _fold_scraper(conn, client, s, posts, lag, a, b)
                out["posts"] += len(posts)
                out["matched"] += matched
                out["added"] += added
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
                     note=f"{s['signature_name']}: {matched} matched, {added} added", by_user=by_user)
            conn.commit()
            return out
        finally:
            conn.close()


def _fold_scraper(conn, client: dict, source: dict, posts: list, lag: int, a, b) -> tuple:
    """Update sheet-listed rows with the scraper's content + counts; insert the
    rest. Returns (matched, added)."""
    matched = added = 0
    now = time.time()
    for p in posts:
        if not p["post_url_norm"]:
            continue
        rows = conn.execute("SELECT id, sheet_date FROM post_metrics WHERE client_id = ? AND post_url_norm = ? "
                            "ORDER BY sheet_date DESC", (client["id"], p["post_url_norm"])).fetchall()
        content = {"display_name": p["display_name"], "handle": p["handle"], "avatar_url": p["avatar_url"],
                   "caption": p["caption"], "media_type": p["media_type"], "thumb_url": p["thumb_url"],
                   "posted_at": p["posted_at"], "collected_at": p["collected_at"]}
        counts = {k: p[k] for k in ("likes", "comments", "shares", "views", "reach")}
        if rows:
            for r in rows:
                sets, vals = [], []
                for k, v in content.items():
                    if v:
                        sets.append(f"{k} = ?"); vals.append(v)
                for k, v in counts.items():
                    if v is not None:
                        sets.append(f"{k} = ?"); vals.append(v)
                sets += ["metric_source = 'scraper'", "status = 'ok'", "skip_reason = ''", "published_at = ?"]
                vals += [now, r["id"]]
                conn.execute(f"UPDATE post_metrics SET {', '.join(sets)} WHERE id = ?", vals)
            matched += 1
        else:
            day = p["day"] or putil.day_str(b)
            if day < putil.day_str(a) or day > putil.day_str(b):
                continue                         # outside the window asked for
            d = putil.parse_day(day)
            conn.execute(
                "INSERT OR IGNORE INTO post_metrics (client_id, project_id, run_id, sheet_date, visible_from, captured_at, "
                "platform, category_raw, handle, display_name, avatar_url, post_url, post_url_norm, post_type, caption, "
                "media_type, thumb_url, posted_at, collected_at, likes, comments, shares, views, reach, impressions, "
                "metric_source, raw_metrics, status, skip_reason, screenshot_path, first_published_at, published_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (client["id"], "", "", day, putil.day_str(putil.add_days(d, lag)), now, p["platform"],
                 p["category_raw"], p["handle"], p["display_name"], p["avatar_url"], p["post_url"], p["post_url_norm"],
                 "", p["caption"], p["media_type"], p["thumb_url"], p["posted_at"], p["collected_at"],
                 p["likes"], p["comments"], p["shares"], p["views"], p["reach"], None,
                 "scraper", json.dumps({"scraper": source.get("signature_name", "")}), "ok", "", "", now, now))
            added += 1
    return matched, added


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
        out = {"sources": len(srcs), "days": 0, "posts": 0, "errors": []}
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
        _log(conn, client_id, "sheet", out["posts"],
             note=f"{out['days']} day(s), {out['sources']} sheet(s)", by_user=by_user)
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
    while not _STOP.wait(60):
        if int(time.time() // 60) % max(1, SYNC_MINUTES) != 0:
            continue
        try:
            for cid, r in sync_sheets_all():
                if r.get("errors"):
                    print(f"[portal] sheet sync {cid}: {'; '.join(r['errors'])}", flush=True)
        except Exception as e:
            print(f"[portal] sheet sync loop error: {e}", flush=True)
        if KEY_SECRET:
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

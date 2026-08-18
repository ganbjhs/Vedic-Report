"""Sources (v3): a project's Google Sheet, watched — new date in, report out.

The loop is deliberately cheap: every `SHEET_SYNC_MINUTES` it re-reads each
enabled sheet source with the smart reader, computes a fingerprint of WHAT
would be captured (links + sections + dates), and does nothing at all when the
fingerprint is unchanged. When it changes — a new date tab appeared, a new date
block was appended, links were added — and `auto_run` is on, it starts a run
with the project's styles, exactly as if someone had pressed Generate. One
thread, no browser, no Google API; the same fetch guards as the New run page.

Nothing here decides which links count for which platform: the rows come out
of `uploads.analyse` like any pasted list, so preview == capture still holds.
"""
import datetime as _dt
import threading
import time

from . import config, runs, smartsheet, uploads
from .jobs import store

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD = None


def _platform_for(project: dict) -> str:
    """The platform the project's styles capture from. If they disagree, use
    'combined' when it is one of them, else the first style's."""
    from . import projects
    plats = [s["platform"] for s in projects.runnable_styles(project)]
    if not plats:
        return "combined"
    if "combined" in plats or len(set(plats)) > 1:
        return "combined"
    return plats[0]


def read_source(src: dict) -> dict:
    """Fetch + understand + analyse. Raises SheetError/UploadError."""
    proj = store.project_get(src["project_id"])
    if proj is None:
        raise ValueError("The project of this source no longer exists.")
    u = smartsheet.read(src["url"], mode=src.get("mode") or "latest",
                        gid=src.get("gid") or None)
    platform = _platform_for(proj)
    want_dedupe = bool((proj.get("settings") or {}).get("dedupe", True))
    report = uploads.analyse(u["grid"], want_dedupe, platform)
    u["analysis"] = report
    u["platform"] = platform
    u["fingerprint"] = smartsheet.fingerprint(u)
    u["project"] = proj
    return u


def _run_name(proj: dict, u: dict) -> str:
    tab = (u.get("tab") or {}).get("name") or ""
    date = u.get("latest_date") or ""
    label = tab or date or _dt.date.today().strftime("%d-%m-%Y")
    return f"{proj['name']} — {label}"[:80]


def check_source(sid: str, force_run: bool = False, user: str = "auto") -> dict:
    """One pass over one source. Returns a summary dict (also written to the
    row). `force_run` starts a run even when nothing changed."""
    src = store.source_get(sid)
    if src is None:
        raise KeyError(sid)
    now = time.time()
    try:
        u = read_source(src)
    except Exception as e:                       # SheetError, UploadError, …
        msg = str(e)
        store.source_update(sid, last_checked_at=now, last_error=msg[:400])
        store.source_log(sid, f"Check failed: {msg}", "error")
        return {"ok": False, "error": msg, "changed": False, "job_ids": []}

    rows = u["analysis"]["rows"]
    changed = u["fingerprint"] != (src.get("last_fingerprint") or "")
    tab = (u.get("tab") or {}).get("name") or ("all tabs" if u.get("mode") == "all" else "")
    # What counts as "new": by default a NEW DATE (a new day tab, or a new date
    # block inside the tab) — the team's sheets grow by the day and one report
    # per day is what they want. 'any_change' also fires when links are added
    # to the current date.
    baseline = bool(src.get("last_fingerprint"))
    new_date = bool(u.get("latest_date")) and (u.get("latest_date") or "") != (src.get("last_date") or "")
    new_tab = bool(tab) and tab != (src.get("last_tab") or "")
    trig = src.get("trigger") or "new_date"
    fire = changed and baseline and (
        (new_date or new_tab) if trig == "new_date" else True)
    fields = {"last_checked_at": now, "last_error": "",
              "last_date": u.get("latest_date") or "", "last_tab": tab,
              "last_count": len(rows)}
    job_ids = []
    if changed:
        fields["last_fingerprint"] = u["fingerprint"]
        fields["last_changed_at"] = now
        what = f"{len(rows)} link(s)"
        if u.get("latest_date"):
            what += f" · date {u['latest_date']}"
        if tab:
            what += f" · tab {tab}"
        store.source_log(sid, f"Sheet changed — {what}.")
    should_run = (fire and src["auto_run"] and rows) or (force_run and rows)
    if should_run:
        proj = u["project"]
        try:
            job_ids = runs.create_run(
                proj, rows, raw=("\n".join(r.get("link", "") for r in rows)).encode("utf-8"),
                upload_name=u.get("source_label") or "Google Sheet",
                report_name=_run_name(proj, u), user=user,
                keep_engagement=bool((proj.get("settings") or {}).get("keep_engagement")),
                workers=int((proj.get("settings") or {}).get("workers") or 0),
                note="Auto-run from sheet source" if not force_run else "Run from sheet source")
            fields["last_job_ids"] = job_ids
            store.source_log(sid, f"Started {len(job_ids)} run(s): {', '.join(job_ids)}.")
        except runs.RunError as e:
            store.source_log(sid, f"Could not start a run: {e}", "error")
            fields["last_error"] = str(e)[:400]
    elif fire and not src["auto_run"]:
        store.source_log(sid, "Auto-run is off — waiting for you to press Run.")
    elif changed and baseline and not fire:
        store.source_log(sid, "Links changed on the current date — no new date yet, so no run "
                              "(switch the trigger to 'any change' to run on this too).")
    elif changed and not rows:
        store.source_log(sid, "Changed, but no capturable links for this project's platform.", "warn")
    store.source_update(sid, **fields)
    return {"ok": True, "changed": changed, "job_ids": job_ids, "count": len(rows),
            "date": u.get("latest_date"), "tab": tab, "notes": u.get("notes") or [],
            "sections": u.get("sections") or [], "shape": u.get("shape"),
            "tabs": u.get("tabs") or [], "dropped": u["analysis"].get("dropped") or []}


def sync_all() -> None:
    """One pass over every enabled source. Serialised, so a slow Google
    answer cannot pile passes on top of each other."""
    if not _LOCK.acquire(blocking=False):
        return
    try:
        for src in store.sources_all(enabled_only=True):
            if _STOP.is_set():
                break
            try:
                check_source(src["id"])
            except Exception as e:                       # rule 17: never silent
                print(f"[sources] {src['id']}: {e}", flush=True)
    finally:
        _LOCK.release()


def _loop() -> None:
    # First pass shortly after boot, then every SHEET_SYNC_MINUTES.
    if _STOP.wait(20):
        return
    while not _STOP.is_set():
        try:
            sync_all()
        except Exception as e:
            print(f"[sources] sync pass failed: {e}", flush=True)
        if _STOP.wait(config.SHEET_SYNC_MINUTES * 60):
            break


def start_scheduler() -> None:
    """Background thread. Not started in inline mode (those hosts stop the CPU
    between requests, so a timer would never fire anyway)."""
    global _THREAD
    if config.EXECUTION_MODE == "inline" or _THREAD is not None:
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="sheet-sync", daemon=True)
    _THREAD.start()
    print(f"[sources] sheet sync every {config.SHEET_SYNC_MINUTES} min", flush=True)


def stop_scheduler() -> None:
    _STOP.set()


def public(src: dict) -> dict:
    return {k: src.get(k) for k in (
        "id", "project_id", "kind", "label", "url", "mode", "gid", "auto_run", "trigger", "enabled",
        "last_date", "last_tab", "last_count", "last_checked_at", "last_changed_at",
        "last_error", "last_job_ids", "log", "created_by", "created_at")}

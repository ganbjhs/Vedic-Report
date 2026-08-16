"""Job API: submit, poll, cancel, download.

Downloads are resolved through the job record and checked against the signed
session's owner — a user can never reach another user's files, and no
user-supplied string is ever used as a path.
"""
import asyncio
import json
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import auth, config, report_types, sheets, uploads
from .jobs import queue, runner, store

router = APIRouter(prefix="/api")

# Tab names observed by the last workbook read, so the preview can offer a
# picker. Request-scoped in practice (one preview per request) and only ever
# used to decorate the response.
_TABS_SEEN = []

_KINDS = {"pdf": "application/pdf",
          "docx": "application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document",
          "html": "text/html; charset=utf-8",
          "xlsx": "application/vnd.openxmlformats-officedocument."
                  "spreadsheetml.sheet",
          "zip": "application/zip"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _owned_job(job_id: str, user: str) -> dict:
    job = store.get(job_id)
    if not job or job["owner"] != user:
        # Same response for "missing" and "someone else's" — don't leak existence.
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def public_job(job: dict) -> dict:
    """The JSON the browser polls. Deliberately excludes paths and the owner."""
    total = job.get("total") or job.get("link_count") or 0
    done = min(job.get("done") or 0, total) if total else (job.get("done") or 0)
    rt = report_types.get(job["report_type"])
    return {
        "id": job["id"],
        "name": job["name"],
        "title": job["title"],
        "report_type": job["report_type"],
        # Label + platform ride along so the UI never has to map a slug itself
        # (an unknown slug — a deleted custom style — still shows something).
        "report_label": rt.label if rt else job["report_type"],
        "platform": rt.platform if rt else report_types.DEFAULT_PLATFORM,
        "keep_engagement": bool(job.get("keep_engagement")),
        "workers": job.get("workers") or 0,
        "status": job["status"],
        "phase": job.get("phase") or "",
        "done": done,
        "total": total,
        "link_count": job.get("link_count") or 0,
        "error": job.get("error") or "",
        "artifacts": sorted((job.get("artifacts") or {}).keys()),
        "skipped": job.get("skipped") or [],
        "activity": job.get("activity") or [],
        "upload_name": job.get("upload_name") or "",
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "elapsed": _elapsed(job),
        "finished": job["status"] in store.DONE_STATES,
        "execution_mode": config.EXECUTION_MODE,
    }


def _elapsed(job: dict) -> int:
    started = job.get("started_at") or job.get("created_at")
    if not started:
        return 0
    end = job.get("finished_at") or time.time()
    return max(0, int(end - started))


async def _read_capped(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over MAX_UPLOAD_MB without buffering
    the whole oversized body."""
    chunks, size = [], 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > config.MAX_UPLOAD_BYTES:
            raise uploads.UploadError(
                f"That file is larger than the {config.MAX_UPLOAD_MB} MB limit.")
        chunks.append(chunk)
    if not size:
        raise uploads.UploadError("No file was uploaded.")
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# Preview — parse an input WITHOUT creating a job
# --------------------------------------------------------------------------- #
async def _grid_from_request(file, text: str, sheet_url: str = "",
                             sheet: str = "") -> tuple:
    """(grid, source_label, original_bytes) for any input method.

    Preview and submit both call this, so what the preview showed is exactly
    what gets captured — they cannot drift apart. `original_bytes` is kept for
    the job folder's troubleshooting copy; for a paste it is the text itself.

    NOTE on sheets: the sheet is fetched again at submit time rather than being
    cached from the preview. Capture time is the honest source of truth, and a
    stale cache would silently capture a version of the sheet the user had
    already changed. The row count is shown at both points so a change is
    visible.
    """
    url = (sheet_url or "").strip()
    if url:
        csv_text = await asyncio.to_thread(sheets.fetch_csv, url)
        grid = uploads.grid_from_csv_text(csv_text)
        if not grid:
            raise uploads.UploadError("That sheet has no readable rows.")
        info = sheets.describe(url)
        label = "Google Sheet" + (f" (tab {info['gid']})" if info.get("gid") else "")
        return grid, label, csv_text.encode("utf-8")

    pasted = (text or "").strip()
    if pasted:
        grid = uploads.grid_from_text(pasted)
        if not grid:
            raise uploads.UploadError(
                "No links found in that text. Paste anything containing post "
                "links — surrounding words are fine.")
        return grid, "pasted links", pasted.encode("utf-8")

    if file is None or not getattr(file, "filename", ""):
        raise uploads.UploadError(
            "Nothing to read — choose a file, paste links, or add a sheet link.")

    suffix = uploads.suffix_of(file.filename)
    raw = await _read_capped(file)
    tmp = config.DATA_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    staged = tmp / f"preview-{int(time.time() * 1000)}{suffix}"
    staged.write_bytes(raw)
    try:
        grid = await asyncio.to_thread(uploads.read_grid, staged, suffix,
                                       sheet or None)
        tabs = await asyncio.to_thread(uploads.list_sheets, staged, suffix)
    finally:
        staged.unlink(missing_ok=True)
    label = uploads.safe_upload_name(file.filename)
    if sheet:
        label += f" · {sheet}"
    _TABS_SEEN.clear()
    _TABS_SEEN.extend(tabs)
    return grid, label, raw


@router.post("/preview")
async def preview(request: Request,
                  file: UploadFile = File(None),
                  text: str = Form(""),
                  sheet_url: str = Form(""),
                  sheet: str = Form(""),
                  link_col: str = Form(""),
                  account_col: str = Form(""),
                  dedupe: str = Form(""),
                  platform: str = Form(report_types.DEFAULT_PLATFORM),
                  csrf_token: str = Form(...),
                  user: str = Depends(auth.require_user_api)):
    """What WOULD be captured, without spending a capture slot.

    The submit path is upload-and-hope: you learn what the tool understood only
    after it has spent browser minutes. This answers the same question for free,
    and is the base every other input method builds on (roadmap A1).
    """
    auth.verify_csrf(request, csrf_token)
    want_dedupe = dedupe.lower() not in ("", "0", "false", "off")
    if not report_types.is_live(platform) or report_types.platform(platform).combines:
        return JSONResponse({"ok": False, "detail": f"{platform!r} is not a "
                             "platform this server can capture yet."}, status_code=400)
    ours = {"x": "X/Twitter", "facebook": "Facebook"}.get(platform, platform)

    try:
        _TABS_SEEN.clear()
        grid, source, _raw = await _grid_from_request(file, text, sheet_url, sheet)
        tabs = list(_TABS_SEEN)
        columns = uploads.detect_columns(grid)
        if link_col != "":
            grid = uploads.reshape(grid, link_col, account_col)
        report = await asyncio.to_thread(uploads.analyse, grid, want_dedupe, platform)
    except (uploads.UploadError, sheets.SheetError) as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False,
             "detail": f"That input could not be read ({e}). Try re-saving it "
                       "as .xlsx or .csv."}, status_code=400)

    rows = report["rows"]
    if not rows:
        detail = (f"Found link(s), but none are {ours} posts — the platform "
                  "picked in step 1 decides which links count."
                  if report["dropped"] else
                  f"No links found. Put one {ours} post URL per row or per "
                  "line, or use a sheet with a column headed 'Link'.")
        if tabs and len(tabs) > 1:
            others = ", ".join(t for t in tabs if t != (sheet or tabs[0]))
            detail += f" This workbook also has: {others} — try another tab."
        # `sheets`/`columns` ride along on the FAILURE too, so the picker can be
        # offered precisely when it is needed. Returning them only on success
        # would leave the user stuck on an empty first tab with no way out.
        return JSONResponse({"ok": False, "detail": detail,
                             "sheets": tabs, "sheet": sheet or "",
                             "columns": columns["columns"],
                             "dropped": report["dropped"][:50]},
                            status_code=400)

    shown = rows[:config.MAX_LINKS]
    return {
        "ok": True,
        "source": source,
        "sheets": tabs,
        "sheet": sheet or (tabs[0] if tabs else ""),
        "columns": columns["columns"],
        "has_header": columns["has_header"],
        "count": len(rows),
        "limit": report["limit"],
        "over_limit": report["over_limit"],
        "dedupe_applied": want_dedupe,
        "duplicate_count": report["duplicate_count"],
        "duplicates": report["duplicates"][:20],
        "dropped_count": len(report["dropped"]),
        "dropped": report["dropped"][:50],
        "rows": [{"n": i, "account": r.get("account_name", ""),
                  "link": r.get("link", "")}
                 for i, r in enumerate(shown, start=1)],
    }


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #
@router.post("/jobs")
async def submit_job(request: Request,
                     file: UploadFile = File(None),
                     text: str = Form(""),
                     sheet_url: str = Form(""),
                     sheet: str = Form(""),
                     link_col: str = Form(""),
                     account_col: str = Form(""),
                     dedupe: str = Form(""),
                     report_name: str = Form(...),
                     report_type: str = Form(...),
                     # Optional, defaulting to the one live platform, so every
                     # existing caller keeps working unchanged.
                     platform: str = Form(report_types.DEFAULT_PLATFORM),
                     csrf_token: str = Form(...),
                     keep_engagement: str = Form(""),
                     workers: str = Form(""),
                     user: str = Depends(auth.require_user_api)):
    auth.verify_csrf(request, csrf_token)

    rt = report_types.get(report_type)
    if rt is None:
        raise HTTPException(status_code=400, detail="Unknown report type.")

    # A disabled pill is a hint; this is the gate. Without it a hand-crafted
    # POST naming a platform with no capture engine would create a job that
    # fails deep inside a runner with an error nobody can act on.
    why_not = report_types.check_runnable(platform, report_type)
    if why_not:
        raise HTTPException(status_code=400, detail=why_not)

    # An unticked checkbox is simply absent from the form body, so anything that
    # arrives means "on". Twitter-only: the influencer capture always keeps the
    # engagement line, so accepting it there would promise a choice that isn't one.
    keep = rt.allows_keep_engagement and keep_engagement.lower() not in (
        "", "0", "false", "off")

    # Capture speed. Clamped, never trusted: each browser is ~0.5-1 GB, so a
    # hand-crafted POST asking for 50 would be an out-of-memory kill rather than
    # a fast report. Anything unparseable means "server default". Twitter-only,
    # for the same reason as the crop tick — see build_command.
    try:
        want_workers = int(workers)
    except ValueError:
        want_workers = 0
    want_workers = (max(0, min(want_workers, config.MAX_WORKERS))
                    if rt.allows_worker_choice else 0)

    # Parse + validate BEFORE a job exists, so a bad input never occupies a slot.
    # Goes through the SAME `_grid_from_request` + `analyse` the preview uses,
    # so what you were shown is exactly what gets captured.
    pasted = (text or "").strip()
    want_dedupe = dedupe.lower() not in ("", "0", "false", "off")
    try:
        grid, source, raw = await _grid_from_request(file, pasted, sheet_url, sheet)
        if link_col != "":
            grid = uploads.reshape(grid, link_col, account_col)
        report = await asyncio.to_thread(uploads.analyse, grid, want_dedupe, platform)
        rows = report["rows"]
        if not rows:
            raise uploads.UploadError(
                f"No {report_types.platform(platform).label} post links found "
                "in that input.")
        if len(rows) > config.MAX_LINKS:
            raise uploads.UploadError(
                f"That input has {len(rows)} links — the limit is "
                f"{config.MAX_LINKS} per job. Split it into smaller batches.")
    except uploads.UploadError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"detail": f"That file could not be read ({e}). Try re-saving it as "
                       ".xlsx or .csv."}, status_code=400)

    stem = uploads.safe_stem(report_name, "Report")
    title = uploads.display_title(report_name, "Report")
    upload_name = (source if (pasted or sheet_url.strip())
                   else uploads.safe_upload_name(file.filename))

    job_id = store.create(owner=user, name=stem, title=title,
                          report_type=report_type, link_count=len(rows),
                          upload_name=upload_name, keep_engagement=keep,
                          workers=want_workers)
    try:
        await asyncio.to_thread(runner.build_job_dir, job_id, rows, raw, upload_name)
    except Exception as e:
        store.update(job_id, status="failed", phase="Failed",
                     error=f"Could not prepare the job folder: {e}")
        return JSONResponse({"detail": f"Could not prepare the job: {e}"},
                            status_code=500)

    store.append_activity(
        job_id, f"Uploaded '{upload_name}' — {len(rows)} X link(s) accepted.")

    if config.EXECUTION_MODE == "inline":
        store.update(job_id, phase="Waiting to start")
    else:
        queue.submit(job_id)

    return JSONResponse({"job_id": job_id, "link_count": len(rows),
                         "execution_mode": config.EXECUTION_MODE}, status_code=202)


# --------------------------------------------------------------------------- #
# Status / cancel
# --------------------------------------------------------------------------- #
@router.get("/jobs")
async def job_list(limit: int = 12, user: str = Depends(auth.require_user_api)):
    """The signed-in user's recent jobs — what the dashboard rail and the
    History page poll. Same shape per job as the status endpoint."""
    limit = max(1, min(int(limit or 12), 200))
    return {"jobs": [public_job(j) for j in store.list_for(user, limit=limit)]}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, user: str = Depends(auth.require_user_api)):
    return public_job(_owned_job(job_id, user))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request,
                     csrf_token: str = Form(...),
                     user: str = Depends(auth.require_user_api)):
    auth.verify_csrf(request, csrf_token)
    _owned_job(job_id, user)
    queue.cancel(job_id)
    return public_job(store.get(job_id))


# --------------------------------------------------------------------------- #
# Inline execution (scale-to-zero hosts)
# --------------------------------------------------------------------------- #
@router.get("/jobs/{job_id}/run-inline")
async def run_inline(job_id: str, user: str = Depends(auth.require_user_api)):
    """Run the capture inside this request and stream progress.

    Needed on hosts that freeze the container once the response is sent (Cloud
    Run), where a fire-and-forget background thread would simply stop. Same
    `runner.run_job` as the queue path — no second implementation.
    """
    job = _owned_job(job_id, user)
    if job["status"] != "queued":
        raise HTTPException(status_code=409,
                            detail=f"Job is already {job['status']}.")

    async def stream():
        """Newline-delimited JSON, one full job status per line.

        Deliberately carries the SAME shape as GET /api/jobs/{id} so the browser
        renders from this stream and never has to poll. That matters on Cloud
        Run: instances auto-scale and each has its own ephemeral SQLite, so a
        poll can land on an instance that has never heard of this job and answer
        404 while the capture is running perfectly on another one. The streaming
        response is pinned to the instance doing the work, so it is the only
        status source that is always right.
        """
        loop = asyncio.get_running_loop()
        bump = asyncio.Event()

        def on_line(_text):
            loop.call_soon_threadsafe(bump.set)

        task = loop.run_in_executor(None, runner.run_job, job_id, on_line)
        last = None
        while True:
            current = await asyncio.to_thread(store.get, job_id)
            if current:
                payload = json.dumps(public_job(current))
                if payload != last:
                    yield payload + "\n"
                    last = payload
            if task.done():
                break
            try:
                await asyncio.wait_for(bump.wait(), timeout=2)
                bump.clear()
            except asyncio.TimeoutError:
                yield "\n"          # keep-alive so proxies don't close the stream

        try:
            await task
        except Exception as e:
            store.update(job_id, status="failed", phase="Failed",
                         error=f"Internal error while running the job: {e}")

        final = await asyncio.to_thread(store.get, job_id)
        if final:
            yield json.dumps(public_job(final)) + "\n"

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        # Buffering would defeat the point — no progress until the job ends.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
@router.get("/jobs/{job_id}/download/{kind}")
async def download(job_id: str, kind: str,
                   user: str = Depends(auth.require_user_api)):
    if kind not in _KINDS:
        raise HTTPException(status_code=404, detail="Unknown download")
    job = _owned_job(job_id, user)
    filename = (job.get("artifacts") or {}).get(kind)
    if not filename:
        raise HTTPException(status_code=404, detail="That file is not available")

    # The stored name is server-generated; resolve it and confirm it really is
    # inside this job's out/ folder before serving.
    out = runner.out_dir(job_id).resolve()
    path = (out / filename).resolve()
    if not str(path).startswith(str(out) + "/") or not path.is_file():
        raise HTTPException(status_code=404, detail="That file is not available")

    # The download is named exactly what the user typed in "Report / File Name",
    # matching the document's own header — spaces and all, since this is a
    # Content-Disposition value and not a path (`path` above is the server's own
    # conservative stem). No date is appended: two reports with the same name are
    # meant to be the same file, and the browser disambiguates a genuine
    # collision itself with "(1)".
    stem = uploads.download_name(job.get("title") or job["name"])
    suffix = "_screenshots.zip" if kind == "zip" else f".{kind}"
    return FileResponse(path, media_type=_KINDS[kind],
                        filename=f"{stem}{suffix}")

"""Job API: submit, poll, cancel, download.

Downloads are resolved through the job record and checked against the signed
session's owner — a user can never reach another user's files, and no
user-supplied string is ever used as a path.
"""
import asyncio
import json
import time
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import auth, config, projects, report_types, runs, sheets, smartsheet, uploads
from .jobs import queue, runner, store

router = APIRouter(prefix="/api")

# Tab names observed by the last workbook read, so the preview can offer a
# picker. Request-scoped in practice (one preview per request) and only ever
# used to decorate the response.
_TABS_SEEN = []
_SHEET_INFO = {}

_KINDS = {"pdf": "application/pdf",
          "docx": "application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document",
          "pptx": "application/vnd.openxmlformats-officedocument."
                  "presentationml.presentation",
          "xlsx": "application/vnd.openxmlformats-officedocument."
                  "spreadsheetml.sheet",
          "zip": "application/zip",
          # The engagement numbers read off the posts, exact — not a report
          # format, so it is never filtered by the user's format choice.
          "csv": "text/csv"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _owned_job(job_id: str, user: str) -> dict:
    """The job, for any signed-in colleague.

    v3: projects are shared by the team, so a run made by one colleague in a
    project is visible — and downloadable — to the next one. Signed-in is the
    gate; a job that does not exist is a 404 like before.
    """
    job = store.get(job_id)
    if not job:
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
        "owner": job.get("owner") or "",
        "project_id": job.get("project_id") or "",
        "report_type": job["report_type"],
        # Label + platform ride along so the UI never has to map a slug itself
        # (an unknown slug — a deleted custom style — still shows something).
        "report_label": rt.label if rt else job["report_type"],
        "platform": rt.platform if rt else report_types.DEFAULT_PLATFORM,
        "keep_engagement": bool(job.get("keep_engagement")),
        "workers": job.get("workers") or 0,
        # What was asked for, resolved: a job saved before 2.4.0 stored nothing
        # and meant "every format this style builds", so History shows that
        # rather than an empty cell.
        "outputs": list(report_types.clean_outputs(job["report_type"],
                                                   job.get("outputs") or ())),
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
                             sheet: str = "", sheet_mode: str = "latest") -> tuple:
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
        # v3: the smart reader — any layout, any tab; `sheet` names a tab
        # (its gid or its name) when the user picked one, `sheet_mode` says
        # whether we want the newest date, that one tab, or all tabs.
        mode = sheet_mode if sheet_mode in ("latest", "tab", "all") else "latest"
        gid = None
        if sheet:
            mode = "tab"
            gid = sheet if sheet.isdigit() else None
            if gid is None:
                for t in await asyncio.to_thread(smartsheet.list_tabs, url):
                    if t["name"] == sheet:
                        gid = t["gid"]
                        break
        u = await asyncio.to_thread(smartsheet.read, url, mode, gid)
        _TABS_SEEN.clear()
        _TABS_SEEN.extend(t["name"] for t in u.get("tabs") or [])
        _SHEET_INFO.clear()
        _SHEET_INFO.update({"tab": (u.get("tab") or {}).get("name") or "",
                            "latest_date": u.get("latest_date"),
                            "sections": u.get("sections") or [],
                            "shape": u.get("shape"), "notes": u.get("notes") or [],
                            "mode": u.get("mode")})
        if len(u["grid"]) <= 1:
            raise uploads.UploadError(
                "No post links found in that sheet" +
                (f" (tab {u['tab']['name']})" if u.get("tab") else "") +
                ". " + " ".join(u.get("notes") or []))
        raw = "\n".join(p["link"] for p in u["posts"]).encode("utf-8")
        return u["grid"], u.get("source_label") or "Google Sheet", raw

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
                  sheet_mode: str = Form("latest"),
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
    if not report_types.is_live(platform):
        return JSONResponse({"ok": False, "detail": f"{platform!r} is not a "
                             "platform this server can capture yet."}, status_code=400)
    ours = {"x": "X/Twitter", "facebook": "Facebook", "instagram": "Instagram",
            "combined": "X / Facebook / Instagram"}.get(platform, platform)

    try:
        _TABS_SEEN.clear()
        _SHEET_INFO.clear()
        grid, source, _raw = await _grid_from_request(file, text, sheet_url, sheet, sheet_mode)
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

    shown = rows[:config.MAX_LINKS] if config.MAX_LINKS else rows
    return {
        "ok": True,
        "source": source,
        "sheets": tabs,
        "sheet": sheet or (_SHEET_INFO.get("tab") if _SHEET_INFO else "") or (tabs[0] if tabs else ""),
        "sheet_info": dict(_SHEET_INFO) if _SHEET_INFO else None,
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
                  "link": r.get("link", ""), "platform": r.get("platform", ""),
                  "section": ("" if r.get("category") in (None, "Uncategorized")
                              else r.get("category")),
                  "metrics": r.get("sheet_metrics") or {}}
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
                     sheet_mode: str = Form("latest"),
                     link_col: str = Form(""),
                     account_col: str = Form(""),
                     dedupe: str = Form(""),
                     report_name: str = Form(...),
                     # One value per style ticked on the New run page. Several
                     # styles = several jobs from the same links, one per style
                     # (each captures on its own today; a shared capture is the
                     # v3.0-b optimisation). A single value keeps every older
                     # caller working unchanged.
                     report_type: List[str] = Form(...),
                     # Optional, defaulting to the one live platform, so every
                     # existing caller keeps working unchanged.
                     platform: str = Form(report_types.DEFAULT_PLATFORM),
                     # v3: which project the run belongs to. Absent = the
                     # session's current project.
                     project_id: str = Form(""),
                     csrf_token: str = Form(...),
                     keep_engagement: str = Form(""),
                     workers: str = Form(""),
                     # One value per ticked box; absent entirely means "every
                     # format this style builds", so an older client keeps
                     # working unchanged.
                     outputs: List[str] = Form(None),
                     user: str = Depends(auth.require_user_api)):
    auth.verify_csrf(request, csrf_token)

    types = [str(t).strip() for t in (report_type or []) if str(t).strip()]
    types = list(dict.fromkeys(types))            # de-dup, keep order
    if not types:
        raise HTTPException(status_code=400, detail="Pick at least one style.")
    if len(types) > 6:
        raise HTTPException(status_code=400, detail="At most 6 styles per run.")
    for t in types:
        if report_types.get(t) is None:
            raise HTTPException(status_code=400, detail=f"Unknown report type {t!r}.")

    project = store.project_get(project_id) if project_id else projects.current(request)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    # The tick boxes are drawn from the style's own outputs, so anything else
    # arriving here was not offered. Refused rather than ignored: a job that
    # quietly dropped a requested format would hand back a file the user did
    # not ask for and say nothing. With several styles the `outputs` list is
    # the union the form collected; each job keeps the ones its style builds
    # (and, if none apply, everything that style builds — clean_outputs).
    asked = [str(o).strip().lower() for o in (outputs or []) if str(o).strip()]
    if len(types) == 1:
        why_not_outputs = report_types.check_outputs(types[0], asked)
        if why_not_outputs:
            raise HTTPException(status_code=400, detail=why_not_outputs)

    # A disabled pill is a hint; this is the gate. Without it a hand-crafted
    # POST naming a platform with no capture engine would create a job that
    # fails deep inside a runner with an error nobody can act on.
    for t in types:
        why_not = report_types.check_runnable(platform, t)
        if why_not:
            raise HTTPException(status_code=400, detail=why_not)

    # An unticked checkbox is simply absent from the form body, so anything that
    # arrives means "on". Twitter-only: the influencer capture always keeps the
    # engagement line, so accepting it there would promise a choice that isn't one.
    keep_flag = keep_engagement.lower() not in ("", "0", "false", "off")

    # Capture speed. Clamped, never trusted: each browser is ~0.5-1 GB, so a
    # hand-crafted POST asking for 50 would be an out-of-memory kill rather than
    # a fast report. Anything unparseable means "server default". Twitter-only,
    # for the same reason as the crop tick — see build_command.
    try:
        want_workers_raw = int(workers)
    except ValueError:
        want_workers_raw = 0

    # Parse + validate BEFORE a job exists, so a bad input never occupies a slot.
    # Goes through the SAME `_grid_from_request` + `analyse` the preview uses,
    # so what you were shown is exactly what gets captured.
    pasted = (text or "").strip()
    want_dedupe = dedupe.lower() not in ("", "0", "false", "off")
    try:
        grid, source, raw = await _grid_from_request(file, pasted, sheet_url, sheet, sheet_mode)
        if link_col != "":
            grid = uploads.reshape(grid, link_col, account_col)
        report = await asyncio.to_thread(uploads.analyse, grid, want_dedupe, platform)
        rows = report["rows"]
        if not rows:
            raise uploads.UploadError(
                f"No {report_types.platform(platform).label} post links found "
                "in that input.")
        if config.MAX_LINKS and len(rows) > config.MAX_LINKS:
            raise uploads.UploadError(
                f"That input has {len(rows)} links — the limit is "
                f"{config.MAX_LINKS} per job. Split it into smaller batches.")
    except (uploads.UploadError, sheets.SheetError) as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"detail": f"That file could not be read ({e}). Try re-saving it as "
                       ".xlsx or .csv."}, status_code=400)

    upload_name = (source if (pasted or sheet_url.strip())
                   else uploads.safe_upload_name(file.filename))
    try:
        job_ids = await runs.create_run_async(
            project, rows, raw, upload_name, report_name, types=types, outputs=asked,
            keep_engagement=keep_flag, workers=want_workers_raw, user=user,
            # Not a tick box on this form: reading the numbers is a property of
            # the report the project wants, not of one submission, and it is set
            # once on Project settings.
            fetch_metrics=bool((project.get("settings") or {}).get("fetch_metrics")),
            fast_capture=bool((project.get("settings") or {}).get("fast_capture")),
            note="Started from New run",
            notes=list(_SHEET_INFO.get("notes") or []) if sheet_url.strip() else None)
    except runs.RunError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    job_id = job_ids[0]

    return JSONResponse({"job_id": job_id, "job_ids": job_ids,
                         "link_count": len(rows), "project_id": project["id"],
                         "execution_mode": config.EXECUTION_MODE}, status_code=202)


# --------------------------------------------------------------------------- #
# Status / cancel
# --------------------------------------------------------------------------- #
@router.get("/jobs")
async def job_list(request: Request, limit: int = 12, project: str = "",
                   user: str = Depends(auth.require_user_api)):
    """Recent jobs of one project (default: the session's current project) —
    what the Runs page polls. Same shape per job as the status endpoint."""
    limit = max(1, min(int(limit or 12), 200))
    proj = store.project_get(project) if project else projects.current(request)
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"project_id": proj["id"],
            "jobs": [public_job(j) for j in store.list_for_project(proj["id"], limit=limit)]}


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

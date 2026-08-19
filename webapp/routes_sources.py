"""Sources API (v3): a project's watched Google Sheets."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from . import auth, projects, sheets, smartsheet, sources
from .jobs import store

router = APIRouter(prefix="/api/projects/{pid}/sources")

_MODES = ("latest", "tab", "all")


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    return data


def _csrf(request: Request, data: dict) -> None:
    auth.verify_csrf(request, str(data.get("csrf_token") or
                                  request.headers.get("x-csrf-token") or ""))


def _project(pid: str) -> dict:
    p = store.project_get(pid)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return p


def _source(pid: str, sid: str) -> dict:
    s = store.source_get(sid)
    if s is None or s["project_id"] != pid:
        raise HTTPException(status_code=404, detail="Source not found.")
    return s


def _styles(project: dict, asked) -> list:
    """The style slugs this source may run — validated against the PROJECT.

    The picker is drawn from the project's own styles, so anything else is a
    hand-crafted request: refused here rather than silently dropped, or the
    source would sit there claiming a style it will never run. Empty is legal
    and means "every style the project has", which is what a source created
    before this existed does.
    """
    if asked is None:
        return None
    if not isinstance(asked, list):
        raise HTTPException(status_code=400, detail="styles must be a list of style slugs.")
    want = [str(x).strip() for x in asked if str(x).strip()]
    if not want:
        return []
    have = {s["slug"] for s in projects.styles_of(project) if not s["missing"]}
    unknown = [w for w in want if w not in have]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"This project does not use {', '.join(sorted(unknown))}. "
                   f"Add it on the Styles page first, or leave the styles blank "
                   f"to run every style the project has.")
    return list(dict.fromkeys(want))


@router.get("")
async def list_sources(pid: str, user: str = Depends(auth.require_user_api)):
    _project(pid)
    return {"sources": [sources.public(s) for s in store.sources_for(pid)]}


@router.post("/inspect")
async def inspect_sheet(pid: str, request: Request,
                        user: str = Depends(auth.require_user_api)):
    """What the reader sees in a sheet BEFORE it is saved: tabs, newest date,
    sections, link count. Body: {url, mode, gid}."""
    data = await _json_body(request)
    _csrf(request, data)
    _project(pid)
    url = str(data.get("url") or "").strip()
    mode = str(data.get("mode") or "latest")
    if mode not in _MODES:
        mode = "latest"
    if not sheets.looks_like_sheet_url(url):
        raise HTTPException(status_code=400, detail="Paste a Google Sheets link.")
    try:
        u = await asyncio.to_thread(smartsheet.read, url, mode, str(data.get("gid") or "") or None)
    except sheets.SheetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read that sheet ({e}).")
    return {"ok": True, "tabs": u["tabs"], "tab": u.get("tab"), "mode": mode,
            "count": len(u["posts"]), "sections": u["sections"], "dates": u["dates"],
            "latest_date": u["latest_date"], "shape": u["shape"], "notes": u["notes"],
            "metric_names": u["metric_names"],
            "sample": [{"section": p["section"], "date": p["date"], "link": p["link"],
                        "account": p["account"]} for p in u["posts"][:12]]}


@router.post("")
async def create_source(pid: str, request: Request,
                        user: str = Depends(auth.require_user_api)):
    data = await _json_body(request)
    _csrf(request, data)
    _project(pid)
    url = str(data.get("url") or "").strip()
    if not sheets.looks_like_sheet_url(url):
        raise HTTPException(status_code=400, detail="Paste a Google Sheets link.")
    try:
        sheets.export_url(url)                    # validates host + shape
    except sheets.SheetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    mode = str(data.get("mode") or "latest")
    if mode not in _MODES:
        raise HTTPException(status_code=400, detail="mode must be latest, tab or all.")
    if len(store.sources_for(pid)) >= 10:
        raise HTTPException(status_code=400, detail="10 sources per project is the ceiling.")
    sid = store.source_create(
        pid, url, mode=mode, gid=str(data.get("gid") or ""),
        auto_run=bool(data.get("auto_run", True)),
        trigger=str(data.get("trigger") or "new_date"),
        styles=_styles(_project(pid), data.get("styles")) or [],
        label=str(data.get("label") or "").strip(), created_by=user)
    # First look right away, so the page shows something — but never a run
    # from the very first read: that would re-generate whatever is already
    # on the sheet the moment it is added. The first read is the baseline.
    result = await asyncio.to_thread(sources.check_source, sid, False, user)
    src = store.source_get(sid)
    if not src.get("last_error"):
        store.source_log(sid, "Added — this is the baseline. New dates or links from now on start a run.")
    return {"ok": True, "source": sources.public(store.source_get(sid)), "check": result,
            "sources": [sources.public(s) for s in store.sources_for(pid)]}


@router.patch("/{sid}")
async def update_source(pid: str, sid: str, request: Request,
                        user: str = Depends(auth.require_user_api)):
    data = await _json_body(request)
    _csrf(request, data)
    src = _source(pid, sid)
    fields = {}
    if "url" in data:
        url = str(data["url"] or "").strip()
        if not sheets.looks_like_sheet_url(url):
            raise HTTPException(status_code=400, detail="Paste a Google Sheets link.")
        try:
            sheets.export_url(url)                # validates host + shape
        except sheets.SheetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if url != (src.get("url") or ""):
            fields["url"] = url
            # A different workbook is a different history: keep the old
            # fingerprint and the first check would either re-run everything or
            # nothing, depending on which sheet happened to be longer.
            fields["last_fingerprint"] = ""
            fields["last_date"] = ""
            fields["last_tab"] = ""
    if "styles" in data:
        fields["styles"] = _styles(_project(pid), data.get("styles")) or []
    if "auto_run" in data:
        fields["auto_run"] = bool(data["auto_run"])
    if "enabled" in data:
        fields["enabled"] = bool(data["enabled"])
    if "label" in data:
        fields["label"] = str(data["label"] or "").strip()[:80]
    if "trigger" in data:
        if str(data["trigger"]) not in ("new_date", "any_change"):
            raise HTTPException(status_code=400, detail="trigger must be new_date or any_change.")
        fields["trigger"] = str(data["trigger"])
    # A new SCOPE is a new baseline — but only when it really changed. The edit
    # form posts every field on every save, so resetting on presence alone would
    # silently re-baseline a source whose label was the only thing touched, and
    # the next check would start a run for a date already reported.
    if "mode" in data:
        if str(data["mode"]) not in _MODES:
            raise HTTPException(status_code=400, detail="mode must be latest, tab or all.")
        if str(data["mode"]) != (src.get("mode") or "latest"):
            fields["mode"] = str(data["mode"])
            fields["last_fingerprint"] = ""
    if "gid" in data and str(data["gid"] or "") != (src.get("gid") or ""):
        fields["gid"] = str(data["gid"] or "")
        fields["last_fingerprint"] = ""
    if not fields:
        return {"ok": True, "source": sources.public(store.source_get(sid))}
    store.source_update(sid, **fields)
    changed = [k for k in fields if not k.startswith("last_")]
    if changed:
        store.source_log(sid, f"Edited by {user}: {', '.join(sorted(changed))}.")
    if fields.get("last_fingerprint") == "":
        store.source_log(sid, "What is watched changed, so this is a new "
                              "baseline — the next new date starts a run.")
    return {"ok": True, "source": sources.public(store.source_get(sid))}


@router.delete("/{sid}")
async def delete_source(pid: str, sid: str, request: Request,
                        user: str = Depends(auth.require_user_api)):
    _csrf(request, {})
    _source(pid, sid)
    store.source_delete(sid)
    return {"ok": True, "sources": [sources.public(s) for s in store.sources_for(pid)]}


@router.post("/{sid}/check")
async def check_now(pid: str, sid: str, request: Request,
                    user: str = Depends(auth.require_user_api)):
    """Sync now: re-read; start a run only if it changed and auto-run is on."""
    _csrf(request, {})
    _source(pid, sid)
    result = await asyncio.to_thread(sources.check_source, sid, False, user)
    return {"ok": True, "check": result, "source": sources.public(store.source_get(sid))}


@router.post("/{sid}/run")
async def run_now(pid: str, sid: str, request: Request,
                  user: str = Depends(auth.require_user_api)):
    """Run now from what the sheet holds, changed or not."""
    _csrf(request, {})
    _source(pid, sid)
    result = await asyncio.to_thread(sources.check_source, sid, True, user)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Could not read the sheet.")
    return {"ok": True, "check": result, "source": sources.public(store.source_get(sid))}

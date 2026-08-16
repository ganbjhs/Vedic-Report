"""Presets and the report-style designer: small JSON APIs behind the dashboard.

Both are pure web-layer conveniences. A preset is a remembered set of form
choices; a style is a profile JSON validated by `profiles/registry.py`. Neither
touches the capture pipeline, and neither can name a report type the runner
would refuse — presets are checked against `report_types` on save, styles
against the registry.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import auth, config, report_types, styles
from .jobs import store

router = APIRouter(prefix="/api")


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


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
def _public_preset(p: dict) -> dict:
    rt = report_types.get(p["report_type"])
    return {
        "id": p["id"], "name": p["name"], "platform": p["platform"],
        "report_type": p["report_type"],
        "report_label": rt.label if rt else p["report_type"],
        "keep_engagement": bool(p.get("keep_engagement")),
        "workers": int(p.get("workers") or 0),
        "dedupe": bool(p.get("dedupe", 1)),
        "sheet_url": p.get("sheet_url") or "",
        "report_name": p.get("report_name") or "",
        "created_at": p.get("created_at"),
    }


@router.get("/presets")
async def list_presets(user: str = Depends(auth.require_user_api)):
    return {"presets": [_public_preset(p) for p in store.presets_for(user)]}


@router.post("/presets")
async def create_preset(request: Request,
                        user: str = Depends(auth.require_user_api)):
    data = await _json_body(request)
    _csrf(request, data)
    name = str(data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the preset a name.")
    platform = str(data.get("platform") or report_types.DEFAULT_PLATFORM)
    report_type = str(data.get("report_type") or "")
    why_not = report_types.check_runnable(platform, report_type)
    if why_not:
        raise HTTPException(status_code=400, detail=why_not)
    rt = report_types.get(report_type)
    try:
        workers = int(data.get("workers") or 0)
    except (TypeError, ValueError):
        workers = 0
    sheet_url = str(data.get("sheet_url") or "").strip()
    if sheet_url and not sheet_url.startswith("https://docs.google.com/"):
        raise HTTPException(status_code=400,
                            detail="Only a Google Sheets link can be saved.")
    if len(store.presets_for(user, limit=100)) >= 50:
        raise HTTPException(status_code=400,
                            detail="You have 50 presets — delete one first.")
    pid = store.preset_create(
        owner=user, name=name, platform=platform, report_type=report_type,
        keep_engagement=bool(data.get("keep_engagement")) and rt.allows_keep_engagement,
        workers=max(0, min(workers, config.MAX_WORKERS)) if rt.allows_worker_choice else 0,
        dedupe=bool(data.get("dedupe", True)), sheet_url=sheet_url,
        report_name=str(data.get("report_name") or "").strip())
    return JSONResponse({"ok": True, "id": pid,
                         "presets": [_public_preset(p) for p in store.presets_for(user)]},
                        status_code=201)


@router.delete("/presets/{pid}")
async def delete_preset(pid: str, request: Request,
                        user: str = Depends(auth.require_user_api)):
    auth.verify_csrf(request, request.headers.get("x-csrf-token") or "")
    if not store.preset_delete(user, pid):
        raise HTTPException(status_code=404, detail="Preset not found.")
    return {"ok": True, "presets": [_public_preset(p) for p in store.presets_for(user)]}


# --------------------------------------------------------------------------- #
# Styles (the designer)
# --------------------------------------------------------------------------- #
@router.get("/styles")
async def list_styles(user: str = Depends(auth.require_user_api)):
    return {"custom": [{"slug": s["slug"], "raw": s["raw"],
                        "label": s["resolved"]["label"]} for s in styles.list_custom()],
            "options": styles.designer_options()}


@router.get("/styles/{slug}")
async def get_style(slug: str, user: str = Depends(auth.require_user_api)):
    try:
        return {"slug": slug, "raw": styles.get_raw(slug),
                "custom": slug not in styles.reserved_slugs()}
    except styles.StyleError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/styles/preview")
async def preview_style(request: Request,
                        user: str = Depends(auth.require_admin)):
    """PNG of an unsaved style. Validation errors come back as JSON 400 with
    the registry's own message, so the designer can show exactly what is wrong."""
    data = await _json_body(request)
    _csrf(request, data)
    try:
        png = styles.preview_png(data.get("profile") or {},
                                 int(data.get("width") or 240))
    except styles.StyleError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    except Exception as e:                            # never a traceback page
        return JSONResponse({"ok": False, "detail": f"Could not draw that: {e}"},
                            status_code=400)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/styles")
async def save_style(request: Request,
                     user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    try:
        resolved = styles.save(data.get("profile") or {},
                               overwrite=bool(data.get("overwrite")))
    except styles.StyleError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    return {"ok": True, "slug": resolved["slug"], "label": resolved["label"]}


@router.delete("/styles/{slug}")
async def delete_style(slug: str, request: Request,
                       user: str = Depends(auth.require_admin)):
    auth.verify_csrf(request, request.headers.get("x-csrf-token") or "")
    try:
        if not styles.delete(slug):
            raise HTTPException(status_code=404, detail="Style not found.")
    except styles.StyleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}

"""Presets and the report-style designer: small JSON APIs behind the dashboard.

Both are pure web-layer conveniences. A preset is a remembered set of form
choices; a style is a profile JSON validated by `profiles/registry.py`. Neither
touches the capture pipeline, and neither can name a report type the runner
would refuse — presets are checked against `report_types` on save, styles
against the registry.
"""
import json
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

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


@router.get("/styles/guide")
async def style_guide(meta: str, page: str = "post",
                      user: str = Depends(auth.require_designer)):
    """Transparent PNG at the page's pixel size, every slot outlined and named.

    A GET so the browser can download it from one link; the meta rides in the
    query string. Nothing is stored and nothing is mutated. DECLARED BEFORE
    `/styles/{slug}` — FastAPI matches in declaration order, and a dynamic
    route above this one would swallow "guide" as a style name.
    """
    m = _meta_object(meta)
    try:
        png = styles.guide_png(m, page)
    except styles.StyleError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    name = f"slot-guide-{(m.get('slug') or m.get('label') or 'style')}-{page}.png"
    return _png(png, re.sub(r"[^A-Za-z0-9._-]+", "-", name))


@router.get("/styles/{slug}")
async def get_style(slug: str, user: str = Depends(auth.require_user_api)):
    try:
        return {"slug": slug, "raw": styles.get_raw(slug),
                "custom": slug not in styles.reserved_slugs()}
    except styles.StyleError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/styles/preview")
async def preview_style(request: Request,
                        user: str = Depends(auth.require_designer)):
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
                     user: str = Depends(auth.require_designer)):
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
                       user: str = Depends(auth.require_designer)):
    auth.verify_csrf(request, request.headers.get("x-csrf-token") or "")
    try:
        if not styles.delete(slug):
            raise HTTPException(status_code=404, detail="Style not found.")
    except styles.StyleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


def _meta_object(meta: str) -> dict:
    try:
        m = json.loads(meta)
        if not isinstance(m, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="meta must be a JSON object.")
    return m


async def _page_files(post, cover, summary, end) -> dict:
    files = {}
    for kind, up in (("post", post), ("cover", cover), ("summary", summary), ("end", end)):
        if up is not None and getattr(up, "filename", ""):
            files[kind] = await up.read()
    return files


@router.post("/styles/template")
async def save_template_style(request: Request,
                              meta: str = Form(...),
                              post: UploadFile = File(None),
                              cover: UploadFile = File(None),
                              summary: UploadFile = File(None),
                              end: UploadFile = File(None),
                              fonts: list[UploadFile] = File(None),
                              overwrite: str = Form(""),
                              csrf_token: str = Form(...),
                              user: str = Depends(auth.require_designer)):
    """A designed-page (Canva) template: page PNGs + the slots drawn on them."""
    auth.verify_csrf(request, csrf_token)
    m = _meta_object(meta)
    files = await _page_files(post, cover, summary, end)
    files["fonts"] = [(up.filename, await up.read()) for up in (fonts or [])
                      if getattr(up, "filename", "")]
    try:
        resolved = styles.save_template(m, files, overwrite=overwrite.lower() in ("1", "true", "on"))
    except styles.StyleError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    return {"ok": True, "slug": resolved["slug"], "label": resolved["label"]}


# --------------------------------------------------------------------------- #
# The design kit — the Canva slot guide and the live page preview.
#
# Both take the meta the designer is editing right now (never a stored style),
# both go through `styles._draft_profile` → `registry.resolve`, and both answer
# a validation error as JSON 400 with the registry's own words. Designer role:
# they render a style's art, which is not something a member may ask for.
# --------------------------------------------------------------------------- #
def _png(data: bytes, filename: str = None) -> Response:
    headers = {"Cache-Control": "no-store"}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(data, media_type="image/png", headers=headers)


@router.post("/styles/guide")
async def style_guide_post(request: Request,
                           user: str = Depends(auth.require_designer)):
    """Same picture for a style whose meta is too big for a URL."""
    data = await _json_body(request)
    _csrf(request, data)
    try:
        png = styles.guide_png(data.get("meta") or {}, str(data.get("page") or "post"))
    except styles.StyleError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    return _png(png)


@router.post("/styles/preview-page")
async def preview_template_page(request: Request,
                                meta: str = Form(...),
                                page: str = Form("post"),
                                post: UploadFile = File(None),
                                cover: UploadFile = File(None),
                                summary: UploadFile = File(None),
                                end: UploadFile = File(None),
                                fonts: list[UploadFile] = File(None),
                                csrf_token: str = Form(...),
                                user: str = Depends(auth.require_designer)):
    """ONE page of the report, rendered from the current slots with sample data
    and a fixture screenshot — the same drawing rules `tpl_builder` uses."""
    auth.verify_csrf(request, csrf_token)
    m = _meta_object(meta)
    uploaded = await _page_files(post, cover, summary, end)
    with tempfile.TemporaryDirectory() as td:
        paths, font_paths = {}, {}
        for kind, data in uploaded.items():
            # The same ceilings the save enforces — a preview must not be the
            # way around them.
            if len(data) > 12 * 1024 * 1024:
                return JSONResponse({"ok": False, "detail": f"The {kind} page image is over 12 MB."},
                                    status_code=400)
            p = Path(td) / f"{kind}.png"
            p.write_bytes(data)
            paths[kind] = p
        for up in (fonts or []):
            if not getattr(up, "filename", ""):
                continue
            data = await up.read()
            if len(data) > 2 * 1024 * 1024:
                return JSONResponse({"ok": False, "detail": f"'{up.filename}' is over 2 MB."},
                                    status_code=400)
            name = Path(up.filename).name
            p = Path(td) / name
            p.write_bytes(data)
            font_paths[name] = str(p)
        try:
            png = styles.page_preview_png(m, paths, str(page or "post"), font_paths)
        except styles.StyleError as e:
            return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "detail": f"Could not draw that: {e}"},
                                status_code=400)
    return _png(png)


@router.get("/styles/{slug}/asset/{kind}")
async def style_asset(slug: str, kind: str, user: str = Depends(auth.require_user_api)):
    """The designed page image of a template style (for the editor + gallery)."""
    if kind not in ("post", "cover", "summary", "end") or not styles._SLUG.match(slug or ""):
        raise HTTPException(status_code=404, detail="No such asset")
    p = styles.asset_dir(slug) / f"{kind}.png"
    if not p.is_file():                       # shipped template styles keep theirs in the repo
        try:
            import registry
            alt = registry.asset_path(registry.load(slug), kind)
            if alt and alt.is_file():
                return FileResponse(alt, media_type="image/png", headers={"Cache-Control": "no-store"})
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="No such asset")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/styles/{slug}/visibility")
async def set_style_visibility(slug: str, request: Request,
                               user: str = Depends(auth.require_admin)):
    """Admin curation: show a style on New report (approve) or take it off."""
    data = await _json_body(request)
    _csrf(request, data)
    try:
        state = styles.set_visible(slug, bool(data.get("show")))
    except styles.StyleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "slug": slug, "visibility": state}


# --------------------------------------------------------------------------- #
# Users (Admin → Users)
# --------------------------------------------------------------------------- #
def _public_users() -> list:
    db = {u["username"]: u for u in store.users_list()}
    out = [{"username": u, "role": r["role"], "source": "app",
            "created_at": r["created_at"], "created_by": r.get("created_by") or ""}
           for u, r in db.items()]
    for u in config.USERS:
        if u not in db:
            out.append({"username": u, "role": auth.role_of(u), "source": ".env",
                        "created_at": None, "created_by": ""})
    return sorted(out, key=lambda x: x["username"])


@router.get("/users")
async def list_users(user: str = Depends(auth.require_admin)):
    return {"users": _public_users(), "roles": list(store.ROLES), "me": user}


@router.post("/users")
async def create_user(request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    name = str(data.get("username") or "").strip().lower()
    pw = str(data.get("password") or "")
    role = str(data.get("role") or "member")
    if not auth.USERNAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Username: 2–31 chars, lowercase letters, digits, . _ -")
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if role not in store.ROLES:
        raise HTTPException(status_code=400, detail="Unknown role.")
    if store.user_get(name) and not data.get("overwrite"):
        raise HTTPException(status_code=400, detail="That user already exists.")
    store.user_upsert(name, auth.hash_password(pw), role, created_by=user)
    return {"ok": True, "users": _public_users()}


@router.patch("/users/{name}")
async def update_user(name: str, request: Request,
                      user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    name = name.strip().lower()
    row = store.user_get(name)
    if not row:
        if name in config.USERS:
            raise HTTPException(status_code=400, detail="That login comes from .env — "
                                "add them here as an app user to manage their role.")
        raise HTTPException(status_code=404, detail="User not found.")
    if "role" in data:
        role = str(data["role"])
        if role not in store.ROLES:
            raise HTTPException(status_code=400, detail="Unknown role.")
        if name == user and role != "admin":
            raise HTTPException(status_code=400, detail="You cannot remove your own admin role.")
        store.user_set_role(name, role)
    if data.get("password"):
        pw = str(data["password"])
        if len(pw) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
        store.user_set_password(name, auth.hash_password(pw))
    return {"ok": True, "users": _public_users()}


@router.delete("/users/{name}")
async def delete_user(name: str, request: Request,
                      user: str = Depends(auth.require_admin)):
    auth.verify_csrf(request, request.headers.get("x-csrf-token") or "")
    name = name.strip().lower()
    if name == user:
        raise HTTPException(status_code=400, detail="You cannot delete yourself.")
    if not store.user_delete(name):
        raise HTTPException(status_code=404, detail="User not found (logins from .env are removed in the file).")
    return {"ok": True, "users": _public_users()}

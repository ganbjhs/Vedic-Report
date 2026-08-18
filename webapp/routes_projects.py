"""Project API (v3): create / switch / edit projects and pick their styles.

Small and boring on purpose. Everything a page needs is a JSON call away, and
the left-bar dropdown, the New project dialog and the project Styles page all
talk to these routes only.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import auth, projects, report_types, styles
from .jobs import store

router = APIRouter(prefix="/api/projects")

_EMOJI_MAX = 8


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


def _listing(request: Request) -> dict:
    cur = projects.current(request)
    return {"projects": [projects.public(p, with_styles=False)
                         for p in projects.all_projects()],
            "current": projects.public(cur)}


@router.get("")
async def list_projects(request: Request, user: str = Depends(auth.require_user_api)):
    return _listing(request)


@router.post("")
async def create_project(request: Request, user: str = Depends(auth.require_user_api)):
    data = await _json_body(request)
    _csrf(request, data)
    name = str(data.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Give the project a name (2+ characters).")
    if len(store.projects_list(include_archived=True)) >= 200:
        raise HTTPException(status_code=400, detail="200 projects is the ceiling — archive some first.")
    slug = projects.unique_slug(name)
    pid = store.project_create(
        owner=user, slug=slug, name=name,
        client=str(data.get("client") or "").strip(),
        emoji=str(data.get("emoji") or "").strip()[:_EMOJI_MAX],
        settings=projects.clean_settings(data.get("settings") or {}))
    # A brand-new project starts with the styles the caller passed, or with
    # nothing — the Styles page is the first stop either way.
    picked = [str(s) for s in (data.get("styles") or []) if report_types.get(str(s))]
    if picked:
        store.project_set_styles(pid, [{"slug": s, "outputs": []} for s in picked])
    projects.select(request, pid)
    return JSONResponse({"ok": True, **_listing(request)}, status_code=201)


@router.post("/{pid}/select")
async def select_project(pid: str, request: Request,
                         user: str = Depends(auth.require_user_api)):
    _csrf(request, {})
    try:
        projects.select(request, pid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"ok": True, **_listing(request)}


@router.get("/{pid}")
async def get_project(pid: str, user: str = Depends(auth.require_user_api)):
    return projects.public(_project(pid))


@router.patch("/{pid}")
async def update_project(pid: str, request: Request,
                         user: str = Depends(auth.require_user_api)):
    data = await _json_body(request)
    _csrf(request, data)
    p = _project(pid)
    fields = {}
    if "name" in data:
        name = str(data["name"] or "").strip()
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="The name needs 2+ characters.")
        fields["name"] = name[:80]
    if "client" in data:
        fields["client"] = str(data["client"] or "").strip()[:80]
    if "emoji" in data:
        fields["emoji"] = str(data["emoji"] or "").strip()[:_EMOJI_MAX]
    if "settings" in data:
        merged = dict(p.get("settings") or {})
        merged.update(projects.clean_settings(data["settings"]))
        fields["settings"] = merged
    if "archived" in data:
        if p["slug"] == store.UNSORTED_SLUG:
            raise HTTPException(status_code=400, detail="The Unsorted project cannot be archived.")
        fields["archived"] = bool(data["archived"])
    store.project_update(pid, **fields)
    return {"ok": True, **_listing(request)}


@router.delete("/{pid}")
async def delete_project(pid: str, request: Request,
                         user: str = Depends(auth.require_user_api)):
    _csrf(request, {})
    p = _project(pid)
    if p["slug"] == store.UNSORTED_SLUG:
        raise HTTPException(status_code=400, detail="The Unsorted project cannot be deleted.")
    if not store.project_delete(pid):
        # Has runs → archive instead, and say so. History is never lost quietly.
        store.project_update(pid, archived=True)
        return {"ok": True, "archived": True, **_listing(request)}
    return {"ok": True, "deleted": True, **_listing(request)}


# --------------------------------------------------------------------------- #
# The project's styles
# --------------------------------------------------------------------------- #
@router.put("/{pid}/styles")
async def set_project_styles(pid: str, request: Request,
                             user: str = Depends(auth.require_user_api)):
    """Replace the list. Body: {"styles": [{"slug": ..., "outputs": [...]}]}.
    Outputs are checked against the style — the same gate the job API has."""
    data = await _json_body(request)
    _csrf(request, data)
    _project(pid)
    items, seen = [], set()
    for raw in (data.get("styles") or []):
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        rt = report_types.get(slug)
        if rt is None:
            raise HTTPException(status_code=400, detail=f"Unknown style {slug!r}.")
        asked = [str(o).strip().lower() for o in (raw.get("outputs") or []) if str(o).strip()]
        why_not = report_types.check_outputs(slug, asked)
        if why_not:
            raise HTTPException(status_code=400, detail=why_not)
        seen.add(slug)
        items.append({"slug": slug,
                      "outputs": list(report_types.clean_outputs(slug, asked))})
    store.project_set_styles(pid, items)
    return {"ok": True, "project": projects.public(_project(pid))}


@router.post("/{pid}/styles/{slug}/background")
async def set_style_background(pid: str, slug: str, request: Request,
                               color: str = Form(""),
                               remove: str = Form(""),
                               image: UploadFile = File(None),
                               csrf_token: str = Form(...),
                               user: str = Depends(auth.require_user_api)):
    """Give a style a page background — a colour or a full-page image — for the
    PDF and the PPTX. Multipart, because an image may ride along.

    A shipped or built-in style cannot be edited (its file lives in the code
    tree), so setting a background on one first COPIES it into a project-owned
    style ("<label> — <project>") and swaps that copy into the project. The
    project keeps printing the same layout, now with its own background.
    """
    auth.verify_csrf(request, csrf_token)
    p = _project(pid)
    if slug not in {s["slug"] for s in store.project_styles(pid)}:
        raise HTTPException(status_code=404, detail="That style is not in this project.")
    rt = report_types.get(slug)
    if rt is None:
        raise HTTPException(status_code=404, detail="Unknown style.")
    if rt.builtin:
        raise HTTPException(status_code=400, detail=(
            f"{rt.label} is a built-in report with its own frozen builder and cannot "
            "take a background. Pick a profile style (e.g. Client deck) instead."))
    data = await image.read() if image is not None and getattr(image, "filename", "") else b""
    try:
        target = slug
        if not rt.custom:
            target = styles.fork_for_project(slug, p)
            store.project_replace_style(pid, slug, target)
        styles.set_background(target, color=color.strip() or None,
                              image=data or None,
                              remove=remove.lower() in ("1", "true", "on"))
    except styles.StyleError as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)
    return {"ok": True, "slug": target, "project": projects.public(_project(pid))}

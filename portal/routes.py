"""Pages and the JSON API. Every data route takes the client from the session
(`auth.require_viewer`) and nothing else — there is no client id in any URL.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import auth, config, db, export, queries, util

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter()


def _asset_version() -> str:
    import hashlib
    h = hashlib.sha1()
    for name in ("static/portal.css", "static/portal.js"):
        try:
            h.update((HERE / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()
templates.env.globals["agency"] = config.AGENCY_NAME
templates.env.globals["base"] = config.BASE_PATH


def _safe_next(raw: str) -> str:
    raw = (raw or "").strip()
    return raw if raw.startswith("/") and not raw.startswith("//") else "/"


# --------------------------------------------------------------------------- #
# Sign in / out / invites
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if auth.session_of(request):
        return RedirectResponse(config.BASE_PATH + _safe_next(next), status_code=303)
    resp = templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": ""})
    return resp


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/")):
    ip = auth.client_ip(request)
    if auth.login_blocked(ip):
        db.audit("login_blocked", email=email, ip=ip)
        return templates.TemplateResponse(request, "login.html",
                                          {"next": _safe_next(next), "error": "Too many attempts. Try again in a few minutes."},
                                          status_code=429)
    user = auth.verify(email, password)
    if not user:
        auth.note_failure(ip)
        db.audit("login_failed", email=(email or "")[:120], ip=ip)
        return templates.TemplateResponse(request, "login.html",
                                          {"next": _safe_next(next), "error": "That e-mail and password do not match."},
                                          status_code=401)
    auth.clear_failures(ip)
    token = auth.start_session(request, user)
    db.audit("login", client_id=user["client_id"], user_id=user["id"], email=user["email"], ip=ip)
    resp = RedirectResponse(config.BASE_PATH + _safe_next(next), status_code=303)
    auth.set_cookie(resp, token)
    return resp


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    got = auth.session_of(request)
    if got:
        s, u, c = got
        auth.verify_csrf(auth.Viewer(s, u, c), csrf_token)
        db.audit("logout", client_id=c["id"], user_id=u["id"], email=u["email"], ip=auth.client_ip(request))
        auth.end_session(request)
    resp = RedirectResponse(config.BASE_PATH + "/login", status_code=303)
    auth.clear_cookie(resp)
    return resp


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    user = auth.user_for_invite(token)
    if not user:
        return templates.TemplateResponse(request, "invite.html", {"token": "", "email": "", "error": "This invitation link has expired or was already used. Ask for a new one."}, status_code=410)
    client = db.client_get(user["client_id"])
    return templates.TemplateResponse(request, "invite.html", {"token": token, "email": user["email"], "client": client, "error": ""})


@router.post("/invite/{token}", response_class=HTMLResponse)
async def invite_submit(request: Request, token: str, password: str = Form(""), confirm: str = Form("")):
    user = auth.user_for_invite(token)
    if not user:
        return templates.TemplateResponse(request, "invite.html", {"token": "", "email": "", "error": "This invitation link has expired or was already used. Ask for a new one."}, status_code=410)
    client = db.client_get(user["client_id"])
    if len(password) < 10:
        return templates.TemplateResponse(request, "invite.html", {"token": token, "email": user["email"], "client": client, "error": "Use at least 10 characters."}, status_code=400)
    if password != confirm:
        return templates.TemplateResponse(request, "invite.html", {"token": token, "email": user["email"], "client": client, "error": "The two passwords differ."}, status_code=400)
    auth.accept_invite(user, password)
    auth.end_all_sessions(user["id"])
    db.audit("invite_accepted", client_id=user["client_id"], user_id=user["id"], email=user["email"], ip=auth.client_ip(request))
    tok = auth.start_session(request, user)
    resp = RedirectResponse(config.BASE_PATH + "/", status_code=303)
    auth.set_cookie(resp, tok)
    return resp


# --------------------------------------------------------------------------- #
# The dashboard
# --------------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, v: auth.Viewer = Depends(auth.require_viewer)):
    m = queries.meta(v.client)
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"meta": m, "client": v.client, "user": v.user, "csrf": v.csrf})


@router.get("/logo")
async def logo(v: auth.Viewer = Depends(auth.require_viewer)):
    p = v.client.get("logo_path") or ""
    f = (config.PORTAL_MEDIA_DIR / p).resolve() if p else None
    if not f or not str(f).startswith(str(config.PORTAL_MEDIA_DIR.resolve())) or not f.is_file():
        raise HTTPException(status_code=404, detail="No logo")
    return FileResponse(str(f))


@router.get("/media/{post_id}")
async def media(post_id: int, v: auth.Viewer = Depends(auth.require_viewer)):
    """A screenshot, only when the client may see them, only for their own
    row, only from inside their media folder."""
    if not v.client.get("show_screenshots"):
        raise HTTPException(status_code=404, detail="Not available")
    r = db.visible_posts(v.client, "AND id = ?", (post_id,))
    if not r or not r[0].get("screenshot_path"):
        raise HTTPException(status_code=404, detail="Not found")
    base = (config.PORTAL_MEDIA_DIR / v.client["slug"]).resolve()
    f = (base / r[0]["screenshot_path"]).resolve()
    if not str(f).startswith(str(base)) or not f.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(f))


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@router.get("/api/reports")
async def api_reports(day: str = "", v: auth.Viewer = Depends(auth.require_viewer)):
    m = queries.meta(v.client)
    d = day or (m["data_through"] or "")
    return {"day": d, "reports": queries.reports_for(v.client, d) if d else []}


@router.get("/report/{report_id}")
async def report_download(report_id: str, v: auth.Viewer = Depends(auth.require_viewer)):
    """One report file, only when the client may see reports, only their own,
    only from inside the media folder, only once visible_from <= today."""
    if not v.client.get("show_reports"):
        raise HTTPException(status_code=404, detail="Not available")
    r = db.report_file(v.client, report_id)
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    base = config.PORTAL_MEDIA_DIR.resolve()
    f = (config.PORTAL_MEDIA_DIR / r["rel_path"]).resolve()
    if not str(f).startswith(str(base)) or not f.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    name = f"{v.client['slug']}-{r['sheet_date']}.{r['fmt']}"
    return FileResponse(str(f), filename=name)


@router.get("/api/meta")
async def api_meta(v: auth.Viewer = Depends(auth.require_viewer)):
    return queries.meta(v.client)


@router.get("/api/daily")
async def api_daily(day: str = "", v: auth.Viewer = Depends(auth.require_viewer)):
    m = queries.meta(v.client)
    day = day or (m["data_through"] or "")
    if not day:
        return {"day": "", "posts": []}
    if m["data_through"] and day > m["data_through"]:
        raise HTTPException(status_code=400, detail=f"Reports go up to {m['data_through']}.")
    try:
        return queries.daily(v.client, day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/trend")
async def api_trend(request: Request, v: auth.Viewer = Depends(auth.require_viewer)):
    q = request.query_params
    a, b = q.get("from") or "", q.get("to") or ""
    m = queries.meta(v.client)
    if not m["data_through"]:
        return {"from": "", "to": "", "days": [], "rows": []}
    b = b or m["data_through"]
    a = a or util.day_str(util.add_days(util.parse_day(b), -6))
    try:
        return queries.trend(v.client, a, b)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/export.xlsx")
async def api_export(request: Request, v: auth.Viewer = Depends(auth.require_viewer)):
    q = request.query_params
    m = queries.meta(v.client)
    if not m["data_through"]:
        raise HTTPException(status_code=404, detail="Nothing published yet.")
    day = q.get("day") or m["data_through"]
    if day > m["data_through"]:
        raise HTTPException(status_code=400, detail=f"Reports go up to {m['data_through']}.")
    t_to = q.get("to") or day
    t_from = q.get("from") or util.day_str(util.add_days(util.parse_day(t_to), -6))
    g_to = q.get("gto") or day
    g_from = q.get("gfrom") or util.day_str(util.add_days(util.parse_day(g_to), -29))
    metric = q.get("metric") or "engagement"
    if metric not in ("engagement", "likes", "comments", "shares", "views"):
        metric = "engagement"
    split = "category" if q.get("split") == "category" else "platform"
    try:
        data = export.build(v.client, day, t_from, t_to, g_from, g_to, metric, split)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.audit("export", client_id=v.client["id"], user_id=v.user["id"], email=v.user["email"],
             detail=f"day={day} trend={t_from}..{t_to} growth={g_from}..{g_to}", ip=auth.client_ip(request))
    name = f"{v.client['slug']}-daily_{day}.xlsx"
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/healthz")
@router.get("/health")
async def healthz():
    return {"ok": True}

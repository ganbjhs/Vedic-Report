"""FastAPI application: login, the upload page, job pages, and wiring.

Run locally:
    .venv/bin/python -m uvicorn webapp.main:app --reload --port 8000
"""
import asyncio
import contextlib
import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import (auth, config, health, previews, projects, report_types,
               routes_extras, routes_jobs, routes_projects, routes_sources,
               sources, styles, uploads, x_login)
from .jobs import cleanup, queue, store

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _asset_version() -> str:
    """Short hash of the CSS + JS contents. Appended to their URLs as ?v=…, so
    a browser that cached the previous build can never pair an old stylesheet
    with new markup — the URL itself changes whenever the file does."""
    import hashlib
    h = hashlib.sha1()
    for name in ("static/app.css", "static/app.js"):
        try:
            h.update((HERE / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    store.init()
    # Rendered from each profile's own config, at boot, so a new or edited
    # profile can never show a stale or missing picture. Four small PNGs;
    # unchanged ones are skipped by hash.
    previews.refresh()
    for warning in config.startup_warnings():
        print(f"[config] WARNING: {warning}", flush=True)
    if config.EXECUTION_MODE != "inline":
        queue.start()
    cleanup.start_scheduler()
    sources.start_scheduler()          # v3: watch every project's sheet sources
    # Free hosts wipe the disk on restart, so sign in to X now (in the
    # background) rather than making the first report wait for it.
    x_login.warm_up_async()
    print(f"[app] ready — mode={config.EXECUTION_MODE} "
          f"jobs={config.MAX_CONCURRENT_JOBS} workers={config.CAPTURE_WORKERS} "
          f"x-auto-login={'on' if x_login.credentials_configured() else 'off'}",
          flush=True)
    yield
    sources.stop_scheduler()
    queue.shutdown()


app = FastAPI(title="Report Automation", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.effective_session_secret(),
    session_cookie="ra_session",
    max_age=config.SESSION_HOURS * 3600,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.include_router(routes_jobs.router)
app.include_router(routes_extras.router)
app.include_router(routes_projects.router)
app.include_router(routes_sources.router)


def _shell(request: Request, user: str, nav: str, **extra) -> dict:
    """Context every signed-in page shares: the top-bar health pills, the
    budget meter in the nav, the CSRF token. One place, so the shell can never
    disagree with itself between pages."""
    role = auth.role_of(user)
    admin = role == "admin"
    # v3: every page hangs off the CURRENT project (left-bar dropdown).
    project = dict(projects.current(request))
    project["is_unsorted"] = project["slug"] == store.UNSORTED_SLUG
    ctx = {"user": user, "csrf": auth.csrf_token(request), "nav": nav,
           "is_admin": admin, "role": role,
           "project": project,
           "projects": projects.all_projects(),
           "project_styles": projects.styles_of(project),
           "can_design": role in ("admin", "designer"),
           # Health and budget are admin concerns; a colleague who only makes
           # reports never sees them (they still get a plain "X capture is
           # unavailable" line when it matters).
           "platform_health": health.platform_health() if admin else [],
           "budget": health.capture_budget() if admin else None,
           "x_login_ok": x_login.session_is_valid()}
    ctx.update(extra)
    return ctx


# --------------------------------------------------------------------------- #
# Redirect signed-out page requests to /login instead of showing a JSON 403
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=exc.headers)
    if exc.status_code == 401:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    user = auth.current_user(request)
    ctx = (_shell(request, user, "") if user else {"user": None})
    ctx.update({"code": exc.status_code, "detail": exc.detail})
    return templates.TemplateResponse(request, "error.html", ctx,
                                      status_code=exc.status_code)


# --------------------------------------------------------------------------- #
# Auth pages
# --------------------------------------------------------------------------- #
def _safe_next(raw: str) -> str:
    """Only allow same-site relative redirects (no open redirect)."""
    raw = (raw or "/").strip()
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if auth.current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"csrf": auth.csrf_token(request), "next": _safe_next(next), "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request,
                       username: str = Form(""),
                       password: str = Form(""),
                       csrf_token: str = Form(""),
                       next: str = Form("/")):
    ip = auth.client_ip(request)
    target = _safe_next(next)

    def fail(message, status=400):
        return templates.TemplateResponse(
            request, "login.html",
            {"csrf": auth.csrf_token(request), "next": target, "error": message},
            status_code=status)

    try:
        auth.verify_csrf(request, csrf_token)
    except HTTPException as e:
        return fail(e.detail)

    if auth.login_blocked(ip):
        return fail(f"Too many failed attempts. Try again in "
                    f"{config.LOGIN_WINDOW_MINUTES} minutes.", 429)

    user = auth.verify_credentials(username, password)
    if not user:
        auth.note_login_failure(ip)
        left = max(0, config.LOGIN_MAX_ATTEMPTS - store.recent_login_failures(ip))
        extra = f" {left} attempt(s) left." if left <= 2 else ""
        return fail("Incorrect username or password." + extra, 401)

    auth.note_login_success(ip)
    auth.login_session(request, user)
    return RedirectResponse(target, status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    with contextlib.suppress(HTTPException):
        auth.verify_csrf(request, csrf_token)
    auth.logout_session(request)
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- #
# App pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def overview(request: Request, user: str = Depends(auth.require_user)):
    """The project's front page: what it prints, its last runs, next step."""
    project = projects.current(request)
    jobs = [routes_jobs.public_job(j)
            for j in store.list_for_project(project["id"], limit=8)]
    return templates.TemplateResponse(
        request, "overview.html",
        _shell(request, user, "overview", jobs=jobs,
               previews=previews.manifest()))


@app.get("/new", response_class=HTMLResponse)
async def new_run(request: Request, user: str = Depends(auth.require_user)):
    """New run: links in, the project's styles out."""
    project = projects.current(request)
    styles_here = projects.styles_of(project)
    return templates.TemplateResponse(
        request, "index.html",
        _shell(request, user, "new",
               max_links=config.MAX_LINKS, max_mb=config.MAX_UPLOAD_MB,
               accept=",".join(uploads.ALLOWED_SUFFIXES),
               default_workers=config.CAPTURE_WORKERS,
               max_workers=config.MAX_WORKERS,
               # The styles offered are the PROJECT's; the pool is one click
               # away when the project has none yet.
               report_types=[s["rt"] for s in styles_here if not s["missing"]],
               chosen_outputs={s["slug"]: s["outputs"] for s in styles_here},
               previews=previews.manifest(),
               platforms=report_types.PLATFORMS))


@app.get("/project/styles", response_class=HTMLResponse)
async def project_styles_page(request: Request, user: str = Depends(auth.require_user)):
    """Which styles this project prints in, picked from the pool."""
    kinds = report_types.all_types()
    return templates.TemplateResponse(
        request, "project_styles.html",
        _shell(request, user, "pstyles",
               pool=kinds, previews=previews.manifest(),
               platforms=report_types.PLATFORMS,
               project_public=projects.public(projects.current(request))))


@app.get("/project/sources", response_class=HTMLResponse)
async def project_sources_page(request: Request, user: str = Depends(auth.require_user)):
    """Where the project's links come from — watched Google Sheets."""
    project = projects.current(request)
    return templates.TemplateResponse(
        request, "project_sources.html",
        _shell(request, user, "sources",
               sources_list=[sources.public(s) for s in store.sources_for(project["id"])],
               sync_minutes=config.SHEET_SYNC_MINUTES))


@app.get("/project/settings", response_class=HTMLResponse)
async def project_settings_page(request: Request, user: str = Depends(auth.require_user)):
    return templates.TemplateResponse(
        request, "project_settings.html",
        _shell(request, user, "psettings", max_workers=config.MAX_WORKERS))


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(job_id: str, request: Request,
                   user: str = Depends(auth.require_user)):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Opening a run switches the session to its project, so the left bar and
    # the Runs page agree with what is on screen.
    if job.get("project_id") and store.project_get(job["project_id"]):
        projects.select(request, job["project_id"])
    return templates.TemplateResponse(
        request, "job.html",
        _shell(request, user, "history", job=routes_jobs.public_job(job)))


@app.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request, user: str = Depends(auth.require_user)):
    """Every run of the current project."""
    project = projects.current(request)
    jobs = [routes_jobs.public_job(j)
            for j in store.list_for_project(project["id"], limit=200)]
    return templates.TemplateResponse(
        request, "history.html", _shell(request, user, "history", jobs=jobs))


@app.get("/history")
async def history_redirect():
    return RedirectResponse("/runs", status_code=301)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(auth.require_admin)):
    return templates.TemplateResponse(
        request, "settings.html",
        _shell(request, user, "settings", settings=config.public_settings(),
               warnings=config.startup_warnings()))


def _session_context(request: Request, user: str, flash=None) -> dict:
    info = x_login.session_state()
    modified = info.get("modified")
    info["modified_text"] = (
        datetime.datetime.fromtimestamp(modified).strftime("%d %b %Y, %H:%M")
        if modified else None)
    last = info.get("last_attempt") or {}
    info["last_attempt_text"] = (
        datetime.datetime.fromtimestamp(last["at"]).strftime("%d %b %Y, %H:%M")
        if last.get("at") else None)
    return _shell(request, user, "accounts", info=info,
                  sessions_dir=str(config.SESSIONS_DIR), flash=flash)


@app.get("/report-types")
async def report_types_redirect():
    return RedirectResponse("/styles", status_code=301)


@app.get("/styles", response_class=HTMLResponse)
async def styles_page(request: Request, user: str = Depends(auth.require_user)):
    """The gallery of report styles, plus the designer for new ones."""
    kinds = report_types.all_types()
    return templates.TemplateResponse(
        request, "styles.html",
        _shell(request, user, "styles",
               report_types=kinds,
               visibility={rt.slug: styles.visibility(rt) for rt in kinds},
               previews=previews.manifest(),
               platforms=report_types.PLATFORMS,
               max_workers=config.MAX_WORKERS))


@app.get("/admin/users", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(auth.require_admin)):
    return templates.TemplateResponse(
        request, "users.html",
        _shell(request, user, "users", users=routes_extras._public_users(),
               roles=list(store.ROLES), env_admins=sorted(config.APP_ADMINS)))


@app.get("/admin/session-status", response_class=HTMLResponse)
async def session_status(request: Request, user: str = Depends(auth.require_admin)):
    """Is a usable X login available, and can the server renew it itself?"""
    return templates.TemplateResponse(
        request, "session_status.html", _session_context(request, user))


@app.post("/admin/x-login", response_class=HTMLResponse)
async def admin_x_login(request: Request, csrf_token: str = Form(""),
                        user: str = Depends(auth.require_admin)):
    """Sign in to X now. Credentials come from the environment, never the form."""
    auth.verify_csrf(request, csrf_token)
    ok, message = await asyncio.to_thread(x_login.force_login)
    return templates.TemplateResponse(
        request, "session_status.html",
        _session_context(request, user,
                         flash={"ok": ok, "message": message}))


# Two names for the same check: /healthz is the conventional one, /health is
# what compose's healthcheck and any external uptime monitor use. Neither
# requires a login and neither does any work.
@app.get("/healthz")
@app.get("/health")
async def healthz():
    return {"ok": True, "mode": config.EXECUTION_MODE}

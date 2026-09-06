"""The Client Portal app.

    .venv/bin/python -m uvicorn portal.main:app --port 8020

No SessionMiddleware here — sessions are rows (portal/auth.py) and the cookie
is opaque. Security headers are set on every response; the CSP allows only
this origin for scripts and styles, and https images (the scraper's
thumbnails live on the platforms' CDNs).
"""
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, routes

HERE = Path(__file__).resolve().parent

_CSP = ("default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'self'")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    for w in config.startup_warnings():
        print(f"[portal] WARNING: {w}", flush=True)
    print(f"[portal] ready — db={config.PORTAL_DB}", flush=True)
    yield


app = FastAPI(title="Client Portal", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.include_router(routes.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api/") else "private")
    return resp


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        return RedirectResponse(f"{config.BASE_PATH}/login?next={request.url.path}", status_code=303)
    return routes.templates.TemplateResponse(request, "error.html",
                                             {"code": exc.status_code, "detail": exc.detail},
                                             status_code=exc.status_code)

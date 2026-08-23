#!/usr/bin/env python3
"""
server.py — WA Toolkit as a web app, served at /wa behind the Report Maker login.

The desktop app (`app.py`) is unchanged in behaviour: it still opens a native
pywebview window and talks to the `Api` class over the pywebview JS bridge.
This module reuses that SAME `Worker` and `Api` — every job, the Playwright
session, the engine, the bot — and only replaces the transport:

    pywebview  window.pywebview.api.foo(a, b)   ->   POST ./api/foo  {"args":[a,b]}

Nothing in `wa/` (session, chat, reader, links, sheets, metrics, engine) is
touched, and nothing in the Report Maker web app (`webapp/`) is touched either.

AUTHENTICATION
--------------
There is no login of its own. Caddy serves this under the same hostname as
Report Maker, so the browser already sends Report Maker's `ra_session` cookie.
That cookie is a Starlette-signed session; mounting SessionMiddleware here with
the SAME SESSION_SECRET lets this app *read* it. It never writes it — the
session dict is only ever read, so Starlette emits no Set-Cookie and Report
Maker's own session is left exactly as it found it.

    signed out  ->  303 to /login?next=/wa/   (Report Maker's own login page)
    signed in   ->  the toolkit

Run:
    WA_DATA_DIR=/data SESSION_SECRET=... uvicorn server:app --host 0.0.0.0 --port 8010
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# NB: importing this chdir()s into DATA_DIR — that is what the desktop app does
# too, and the relative paths in config.json (file1.txt, plugins/, wa_profile)
# depend on it.
import app as desktop                                    # noqa: E402
from wa.paths import APP_DIR, CONFIG, DATA_DIR           # noqa: E402
from wa.session import SEL, WASession                    # noqa: E402

UI_DIR = APP_DIR / "ui"
QR_PATH = DATA_DIR / "qr.png"

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
SESSION_COOKIE = os.environ.get("SESSION_COOKIE", "ra_session").strip()
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12") or 12)
COOKIE_SECURE = (os.environ.get("COOKIE_SECURE", "") or "0").strip().lower() in (
    "1", "true", "yes", "on")
ROOT_PATH = os.environ.get("WA_ROOT_PATH", "/wa").rstrip("/")
LOGIN_URL = os.environ.get("WA_LOGIN_URL", "/login")
# A server has no screen; a headed Chromium cannot start. Forced on by default.
FORCE_HEADLESS = (os.environ.get("WA_FORCE_HEADLESS", "1") or "1").strip().lower() in (
    "1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Worker — the desktop one, plus a login that works without a screen
# --------------------------------------------------------------------------- #
class WebWorker(desktop.Worker):
    """desktop.Worker with a QR login that a remote browser can complete.

    `Worker.job_login` opens a visible window and blocks in `wait_logged_in`
    for up to five minutes — fine on a laptop, useless on a VPS. This variant
    runs headless and writes a screenshot of the login page to `qr.png` every
    two seconds while it waits, which `GET ./qr.png` serves to the operator's
    browser. The screenshot is taken on THIS thread: Playwright objects belong
    to the thread that created them, so the HTTP handlers must never touch the
    page themselves.
    """

    qr_state = "idle"        # idle | waiting | logged_in | failed

    def job_login_web(self, timeout_s: int = 300):
        self.close_browser()
        cfg = desktop.load_json(CONFIG)
        self.qr_state = "waiting"
        self.emit("Launching WhatsApp Web (headless)…")
        session = WASession(cfg.get("profile_dir", "./wa_profile"), headless=True)
        session.__enter__()
        self.session = session
        deadline = time.time() + int(timeout_s)
        while time.time() < deadline:
            if self.stop_flag:
                self.qr_state = "idle"
                raise KeyboardInterrupt
            for css in SEL["logged_in"]:
                try:
                    if session.page.locator(css).count():
                        self.qr_state = "logged_in"
                        try:
                            QR_PATH.unlink()
                        except OSError:
                            pass
                        self.emit("Logged in.", "ok")
                        time.sleep(1.5)          # let the chat list settle
                        return "ok"
                except Exception:                # noqa: BLE001  page mid-navigation
                    pass
            try:
                session.page.screenshot(path=str(QR_PATH), full_page=False)
            except Exception:                    # noqa: BLE001
                pass
            time.sleep(2)
        self.qr_state = "failed"
        raise TimeoutError(
            "Nobody scanned the QR code within "
            f"{int(timeout_s) // 60} minutes. Press 'Log in' and try again.")


# --------------------------------------------------------------------------- #
# Api — the desktop one, minus the two calls that need a desktop
# --------------------------------------------------------------------------- #
class WebApi(desktop.Api):
    """desktop.Api with the native-dialog calls replaced.

    `pick_file` drives an OS file-open dialog and `open_folder` shells out to
    `open`/`xdg-open`; neither means anything on a server. The UI calls
    `pick_file` from a real <input type=file> now and uploads to ./upload, so
    the method is kept only to return a clear message if something still calls it.
    """

    def pick_file(self):
        return {"error": "Use the Choose file button — the server has no file dialog."}

    def open_folder(self):
        return {"error": f"Files live on the server, in {DATA_DIR}."}

    # ---- login / QR ------------------------------------------------------
    def login(self):
        """Overrides the desktop login: headless + QR screenshot polling."""
        self.w.jobs.put({"kind": "login_web", "payload": {}})
        return "queued"

    def qr_state(self):
        return {"state": getattr(self.w, "qr_state", "idle"),
                "has_image": QR_PATH.exists(),
                "browser": self.w.session is not None}

    def where(self):
        return {"data_dir": str(DATA_DIR), "app_dir": str(APP_DIR)}


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def _force_headless_config() -> None:
    """A VPS has no display. Flip config.json once, loudly, rather than letting
    Chromium fail to launch with an error nobody can read."""
    if not FORCE_HEADLESS:
        return
    cfg = desktop.load_json(CONFIG)
    if cfg.get("headless") is not True:
        cfg["headless"] = True
        desktop.save_json(CONFIG, cfg)
        print("[wa] config.json: headless -> true (no display on a server)", flush=True)


_force_headless_config()

worker = WebWorker()
worker.start()
api = WebApi(worker)

# Every public method of Api is callable over HTTP; nothing else is.
METHODS = {
    name for name, fn in inspect.getmembers(api, predicate=callable)
    if not name.startswith("_")
}

app = FastAPI(title="WA Toolkit", root_path=ROOT_PATH, docs_url=None, redoc_url=None)

# Same cookie, same secret, same options as webapp/main.py — this app only ever
# READS the session, so Starlette never sends a Set-Cookie and Report Maker's
# own login is untouched. A missing or mismatched secret fails closed: the
# cookie will not verify, the session reads empty, and the user is sent to /login.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "unset-secret-nothing-will-verify",
    session_cookie=SESSION_COOKIE,
    max_age=SESSION_HOURS * 3600,
    same_site="lax",
    https_only=COOKIE_SECURE,
)


def _user(request: Request):
    try:
        return request.session.get("user")
    except Exception:            # noqa: BLE001  middleware not reached
        return None


def _require_page(request: Request) -> str:
    user = _user(request)
    if not user:
        raise HTTPException(status_code=303, detail="login required",
                            headers={"Location": f"{LOGIN_URL}?next={ROOT_PATH}/"})
    return user


def _require_api(request: Request) -> str:
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


@app.get("/health")
def health():
    """Unauthenticated on purpose — Docker's healthcheck has no cookie."""
    return {"ok": True, "status": worker.status, "browser": worker.session is not None}


@app.get("/")
def index(request: Request):
    _require_page(request)
    return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/bundle.js")
def bundle_js(request: Request):
    _require_page(request)
    return FileResponse(UI_DIR / "bundle.js", media_type="application/javascript")


@app.get("/bundle.css")
def bundle_css(request: Request):
    _require_page(request)
    return FileResponse(UI_DIR / "bundle.css", media_type="text/css")


@app.get("/qr.png")
def qr_png(request: Request):
    _require_api(request)
    if not QR_PATH.exists():
        raise HTTPException(status_code=404, detail="No QR image yet")
    return FileResponse(QR_PATH, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """Replaces Api.pick_file — the browser sends the .txt, we hand back the
    same {path, content} shape the UI already knows how to use."""
    _require_api(request)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File larger than 5 MB")
    return {"path": file.filename, "content": raw.decode("utf-8", errors="replace")}


@app.post("/api/{method}")
def call(method: str, request: Request, payload: dict = Body(default=None)):
    """One endpoint per Api method, dispatched by name.

    Guarded by the `X-WA-Api` header as well as the cookie: the session cookie
    is SameSite=Lax, so another site cannot make the browser send it on a POST,
    and a header this specific cannot be set by a plain cross-site form.
    """
    _require_api(request)
    if request.headers.get("x-wa-api") != "1":
        raise HTTPException(status_code=400, detail="Missing X-WA-Api header")
    if method not in METHODS:
        raise HTTPException(status_code=404, detail=f"No such method: {method}")
    args = (payload or {}).get("args") or []
    if not isinstance(args, list):
        raise HTTPException(status_code=400, detail="'args' must be a list")
    try:
        return JSONResponse({"ok": True, "result": getattr(api, method)(*args)})
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"{method}: {e}") from e
    except Exception as e:                        # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"{type(e).__name__}: {e}") from e


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException):
    """303s carry a Location and must redirect; everything else stays JSON so
    the UI's fetch() wrapper can surface the message in a toast."""
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)

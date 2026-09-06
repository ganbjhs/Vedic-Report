"""Portal settings from the environment (`.env` is read by the process that
starts uvicorn — docker compose's env_file, or `python-dotenv` locally).

Nothing here is shared with `webapp/config.py` on purpose: the portal has its
own secret, its own cookie, its own database path. The one value both apps
must agree on is PORTAL_DB (where the file is) and PORTAL_KEY_SECRET (the
scraper keys are sealed with it).
"""
import os
import secrets
from pathlib import Path

try:                                     # optional: read .env when run by hand
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


def _path(name: str, default: str) -> Path:
    v = os.environ.get(name, "").strip()
    p = Path(v) if v else (ROOT / default)
    return p if p.is_absolute() else (ROOT / p)


PORTAL_DB = _path("PORTAL_DB", "data/portal.db")
PORTAL_MEDIA_DIR = _path("PORTAL_MEDIA_DIR", "data/portal/media")
PORTAL_SESSION_SECRET = os.environ.get("PORTAL_SESSION_SECRET", "").strip()
PORTAL_KEY_SECRET = os.environ.get("PORTAL_KEY_SECRET", "").strip()
PORTAL_HOST = os.environ.get("PORTAL_HOST", "").strip()          # clients.vedictech.in
PORTAL_TZ = os.environ.get("PORTAL_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
PORTAL_LAG_DAYS_DEFAULT = int(os.environ.get("PORTAL_LAG_DAYS_DEFAULT", "2") or 2)
COOKIE_SECURE = _bool("PORTAL_COOKIE_SECURE", _bool("COOKIE_SECURE", False))
SESSION_HOURS = int(os.environ.get("PORTAL_SESSION_HOURS", "24") or 24)
LOGIN_MAX_ATTEMPTS = int(os.environ.get("PORTAL_LOGIN_MAX_ATTEMPTS", "8") or 8)
LOGIN_WINDOW_MINUTES = int(os.environ.get("PORTAL_LOGIN_WINDOW_MINUTES", "15") or 15)
INVITE_HOURS = int(os.environ.get("PORTAL_INVITE_HOURS", "72") or 72)
MAX_TREND_DAYS = int(os.environ.get("PORTAL_MAX_TREND_DAYS", "120") or 120)
AGENCY_NAME = os.environ.get("PORTAL_AGENCY_NAME", "Vedic Tech").strip() or "Vedic Tech"

# Serve under a path prefix on a shared host (e.g. report.vedictech.in/portal,
# with Caddy stripping the prefix): set PORTAL_BASE_PATH=/portal. Every link the
# app emits is prefixed with it and the cookie is scoped to it. Empty (default)
# serves at the root, e.g. on its own subdomain clients.vedictech.in.
BASE_PATH = ("/" + os.environ.get("PORTAL_BASE_PATH", "").strip().strip("/")).rstrip("/")

_EPHEMERAL = secrets.token_urlsafe(48)


def session_secret() -> str:
    return PORTAL_SESSION_SECRET or _EPHEMERAL


def startup_warnings() -> list:
    out = []
    if not PORTAL_SESSION_SECRET:
        out.append("PORTAL_SESSION_SECRET is not set — every restart signs clients out.")
    if not PORTAL_KEY_SECRET:
        out.append("PORTAL_KEY_SECRET is not set — scraper API keys cannot be opened, "
                   "so the scraper sync is off.")
    if not COOKIE_SECURE:
        out.append("PORTAL_COOKIE_SECURE is off — fine locally, set it to 1 behind HTTPS.")
    return out

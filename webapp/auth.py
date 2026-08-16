"""Authentication: username + password from `.env`, signed session cookie,
CSRF tokens, and login rate limiting.

No OAuth, no external identity provider — self-contained, as specified.
Credentials never leave the server; the browser only ever holds an opaque
signed session cookie.
"""
import hashlib
import hmac
import re
import secrets

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from . import config
from .jobs import store


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
_PBKDF2_ITERS = 240_000
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}$")


def hash_password(password: str) -> str:
    """pbkdf2-sha256, stdlib only — the format users created in the app get.
    (bcrypt from .env keeps working; nothing new is bcrypt-hashed.)"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), _PBKDF2_ITERS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt}${digest}"


def _check_password(stored: str, given: str) -> bool:
    """Constant-time compare. `$2…` = bcrypt (from .env), `pbkdf2_sha256$…` =
    app-created user, anything else = plaintext from .env."""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt, digest = stored.split("$", 3)
            calc = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"),
                                       salt.encode("utf-8"), int(iters)).hex()
            return hmac.compare_digest(calc, digest)
        except (ValueError, TypeError):
            return False
    if stored.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(given.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    return hmac.compare_digest(stored, given)


def verify_credentials(username: str, password: str):
    """Return the canonical username on success, else None.

    App-managed users (DB) first, then `.env` APP_USERS. Always runs a
    comparison, even for an unknown user, so timing doesn't reveal usernames.
    """
    username = (username or "").strip()
    row = store.user_get(username) if username else None
    stored = row["pw_hash"] if row else config.USERS.get(username)
    ok = _check_password(stored, password or "") if stored else _check_password(
        "$2b$12$" + "x" * 53, password or "")
    return username if (stored and ok) else None


# --------------------------------------------------------------------------- #
# Roles: admin (everything), designer (may create/edit styles, which stay
# pending until an admin shows them), member (makes reports).
# --------------------------------------------------------------------------- #
def role_of(username: str) -> str:
    row = store.user_get(username) if username else None
    if row:
        return row["role"] if row["role"] in store.ROLES else "member"
    return "admin" if config.is_admin(username) else "member"


def is_admin(username: str) -> bool:
    return role_of(username) == "admin"


def can_design(username: str) -> bool:
    return role_of(username) in ("admin", "designer")


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def login_session(request: Request, username: str) -> None:
    request.session.clear()
    request.session["user"] = username
    request.session["csrf"] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    request.session.clear()


def current_user(request: Request):
    return request.session.get("user")


def require_user(request: Request) -> str:
    """FastAPI dependency for HTML pages — redirects to /login when signed out."""
    user = current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="login required",
            headers={"Location": f"/login?next={request.url.path}"})
    return user


def require_user_api(request: Request) -> str:
    """FastAPI dependency for JSON endpoints — 401 instead of a redirect."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def require_admin(request: Request) -> str:
    """Pages/APIs for the people who run the server. Everyone else gets 403 —
    the link is simply not shown to them, but hiding is not a gate."""
    user = require_user(request) if not request.url.path.startswith("/api/") \
        else require_user_api(request)
    if not is_admin(user):
        raise HTTPException(status_code=403,
                            detail="This page is for administrators. Ask the "
                                   "person who runs Report Maker.")
    return user


def require_designer(request: Request) -> str:
    """Admins and designers: the style designer and its APIs."""
    user = require_user(request) if not request.url.path.startswith("/api/") \
        else require_user_api(request)
    if not can_design(user):
        raise HTTPException(status_code=403,
                            detail="Designing styles needs the designer role — "
                                   "ask an admin to grant it.")
    return user


def redirect_to_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("csrf") or ""
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=400,
                            detail="This form expired. Reload the page and try again.")


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str:
    """Client IP, honouring X-Forwarded-For (we sit behind Caddy / Cloud Run)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_blocked(ip: str) -> bool:
    return store.recent_login_failures(ip) >= config.LOGIN_MAX_ATTEMPTS


def note_login_failure(ip: str) -> None:
    store.record_login_failure(ip)


def note_login_success(ip: str) -> None:
    store.clear_login_failures(ip)

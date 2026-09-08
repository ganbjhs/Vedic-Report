"""Client sign-in: invite links, passwords, server-side sessions, rate limit.

* Passwords: pbkdf2-sha256, 240k iterations — the same helper the internal
  tool uses (copied, not imported: the portal must not import `webapp`).
* Sessions live in `client_sessions`; the cookie holds a random token whose
  SHA-256 is the row key. Revoking a session is deleting a row. Nothing about
  the user is in the cookie.
* Invites: the admin creates a user with an invite token (hashed at rest).
  The link `/invite/<token>` lets the person set a password once, within
  PORTAL_INVITE_HOURS.
* Rate limit: PORTAL_LOGIN_MAX_ATTEMPTS failures per IP in
  PORTAL_LOGIN_WINDOW_MINUTES → the form refuses, without saying whether the
  e-mail exists.
"""
import hashlib
import hmac
import re
import secrets
import time
import uuid

from fastapi import HTTPException, Request, status

from . import config, db

_PBKDF2_ITERS = 240_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COOKIE = "portal_sid"
ROLES = ("viewer", "manager")


# --------------------------------------------------------------------------- #
# Passwords + tokens
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), _PBKDF2_ITERS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt}${digest}"


def check_password(stored: str, given: str) -> bool:
    try:
        _, iters, salt, digest = (stored or "").split("$", 3)
        calc = hashlib.pbkdf2_hmac("sha256", (given or "").encode("utf-8"),
                                   salt.encode("utf-8"), int(iters)).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password("timing-equaliser")


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_invite(user_id: str, hours: int = None) -> str:
    """Set a fresh invite token on the user row; returns the RAW token for the link."""
    token = secrets.token_urlsafe(32)
    db.rw().execute("UPDATE client_users SET invite_token = ?, invite_expires = ? WHERE id = ?",
                    (token_hash(token), time.time() + 3600 * (hours or config.INVITE_HOURS), user_id))
    db.rw().commit()
    return token


def user_for_invite(token: str):
    u = db.one("SELECT * FROM client_users WHERE invite_token = ? AND disabled = 0", (token_hash(token),))
    if not u or not u.get("invite_expires") or u["invite_expires"] < time.time():
        return None
    return u


def accept_invite(user: dict, password: str) -> None:
    db.rw().execute("UPDATE client_users SET pw_hash = ?, invite_token = '', invite_expires = NULL "
                    "WHERE id = ?", (hash_password(password), user["id"]))
    db.rw().commit()


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def verify(login: str, password: str):
    """The user row on success, else None. `login` is the e-mail OR the
    staff-assigned username. Always runs one comparison so the response time
    does not reveal whether the account exists."""
    login = (login or "").strip().lower()
    u = db.one("SELECT * FROM client_users WHERE (lower(email) = ? OR (username <> '' AND lower(username) = ?)) "
               "AND disabled = 0", (login, login)) if login else None
    ok = check_password(u["pw_hash"], password) if (u and u.get("pw_hash")) else check_password(_DUMMY, password or "")
    return u if (u and u.get("pw_hash") and ok) else None


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}$")


def set_login(conn, client_id: str, email: str, password: str, username: str = "",
              role: str = "viewer", by: str = "") -> dict:
    """Create or update a client user with a password STAFF set directly — no
    invite link. e-mail is the account id (kept, unique per client); username
    is an optional global login alias. Returns {id, email, username}."""
    email = (email or "").strip().lower()
    username = (username or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("That is not an e-mail address.")
    if username and not USERNAME_RE.match(username):
        raise ValueError("Username: a letter or digit, then letters, digits, . _ - (2-31 chars).")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    role = role if role in ROLES else "viewer"
    if username:
        clash = conn.execute("SELECT id FROM client_users WHERE lower(username) = ? AND client_id <> ?",
                             (username, client_id)).fetchone()
        # a username already taken under a DIFFERENT client would break login routing
        row2 = conn.execute("SELECT id FROM client_users WHERE lower(username) = ? AND client_id = ?",
                            (username, client_id)).fetchone()
        if clash and not row2:
            raise ValueError("That username is already taken.")
    row = conn.execute("SELECT id FROM client_users WHERE client_id = ? AND lower(email) = ?",
                       (client_id, email)).fetchone()
    if row:
        uid = row["id"]
        conn.execute("UPDATE client_users SET username = ?, pw_hash = ?, role = ?, invite_token = '', "
                     "invite_expires = NULL, disabled = 0 WHERE id = ?",
                     (username, hash_password(password), role, uid))
    else:
        uid = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO client_users (id, client_id, email, username, pw_hash, role, invited_by, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (uid, client_id, email, username, hash_password(password), role, by, time.time()))
    conn.commit()
    return {"id": uid, "email": email, "username": username}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def start_session(request: Request, user: dict) -> str:
    """Create the row; return the cookie value (raw token)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.rw().execute("INSERT INTO client_sessions (sid, user_id, client_id, csrf, created_at, last_seen_at, ip, ua) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (token_hash(token), user["id"], user["client_id"], secrets.token_urlsafe(24), now, now,
                     client_ip(request), (request.headers.get("user-agent") or "")[:200]))
    db.rw().execute("UPDATE client_users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
    db.rw().commit()
    return token


def end_session(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.rw().execute("DELETE FROM client_sessions WHERE sid = ?", (token_hash(token),))
        db.rw().commit()


def end_all_sessions(user_id: str) -> None:
    db.rw().execute("DELETE FROM client_sessions WHERE user_id = ?", (user_id,))
    db.rw().commit()


def session_of(request: Request):
    """(session, user, client) for a valid cookie, else None. Expired sessions
    are dropped here; `last_seen_at` is bumped at most once a minute."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    s = db.one("SELECT * FROM client_sessions WHERE sid = ?", (token_hash(token),))
    if not s:
        return None
    now = time.time()
    if now - s["last_seen_at"] > config.SESSION_HOURS * 3600:
        db.rw().execute("DELETE FROM client_sessions WHERE sid = ?", (s["sid"],))
        db.rw().commit()
        return None
    u = db.one("SELECT * FROM client_users WHERE id = ? AND disabled = 0", (s["user_id"],))
    c = db.client_get(s["client_id"]) if u else None
    if not u or not c:
        return None
    if now - s["last_seen_at"] > 60:
        db.rw().execute("UPDATE client_sessions SET last_seen_at = ? WHERE sid = ?", (now, s["sid"]))
        db.rw().commit()
    return s, u, c


def set_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=config.SESSION_HOURS * 3600, httponly=True,
                        samesite="lax", secure=config.COOKIE_SECURE, path=(config.BASE_PATH or "/"))


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, path=(config.BASE_PATH or "/"))


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
class Viewer:
    def __init__(self, session: dict, user: dict, client: dict):
        self.session, self.user, self.client = session, user, client

    @property
    def csrf(self) -> str:
        return self.session["csrf"]


def require_viewer(request: Request) -> Viewer:
    got = session_of(request)
    if not got:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Not signed in")
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, detail="login required",
                            headers={"Location": f"{config.BASE_PATH}/login?next={request.url.path}"})
    return Viewer(*got)


def verify_csrf(viewer: Viewer, submitted: str) -> None:
    if not submitted or not hmac.compare_digest(viewer.csrf, submitted):
        raise HTTPException(status_code=400, detail="This form expired. Reload the page and try again.")


# --------------------------------------------------------------------------- #
# Rate limiting (per IP)
# --------------------------------------------------------------------------- #
def login_blocked(ip: str) -> bool:
    since = time.time() - config.LOGIN_WINDOW_MINUTES * 60
    r = db.one("SELECT COUNT(*) AS n FROM client_login_attempts WHERE ip = ? AND at > ?", (ip, since))
    return bool(r and r["n"] >= config.LOGIN_MAX_ATTEMPTS)


def note_failure(ip: str) -> None:
    db.rw().execute("INSERT INTO client_login_attempts (ip, at) VALUES (?, ?)", (ip, time.time()))
    db.rw().execute("DELETE FROM client_login_attempts WHERE at < ?", (time.time() - 86400,))
    db.rw().commit()


def clear_failures(ip: str) -> None:
    db.rw().execute("DELETE FROM client_login_attempts WHERE ip = ?", (ip,))
    db.rw().commit()


# --------------------------------------------------------------------------- #
# Creating users (used by the internal tool's Admin → Clients too)
# --------------------------------------------------------------------------- #
def create_user(conn, client_id: str, email: str, role: str = "viewer", invited_by: str = "") -> dict:
    """Insert (or re-invite) a user on any rw connection to portal.db; returns
    {id, token} — the raw token goes into the invite link exactly once."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("That is not an e-mail address.")
    role = role if role in ROLES else "viewer"
    row = conn.execute("SELECT id FROM client_users WHERE client_id = ? AND lower(email) = ?",
                       (client_id, email)).fetchone()
    token = secrets.token_urlsafe(32)
    expires = time.time() + 3600 * config.INVITE_HOURS
    if row:
        uid = row["id"]
        conn.execute("UPDATE client_users SET role = ?, invite_token = ?, invite_expires = ?, disabled = 0 "
                     "WHERE id = ?", (role, token_hash(token), expires, uid))
    else:
        uid = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO client_users (id, client_id, email, pw_hash, role, invited_by, invite_token, "
                     "invite_expires, created_at) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?)",
                     (uid, client_id, email, role, invited_by, token_hash(token), expires, time.time()))
    conn.commit()
    return {"id": uid, "token": token, "email": email}

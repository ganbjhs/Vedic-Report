"""Connections, the visibility rule, and the audit log.

Two connections to the same file:

* `ro()` — SQLite `mode=ro`. Everything the client sees is read through it,
  so a bug in a query can never turn into a write.
* `rw()` — for the portal's OWN tables only: sessions, login attempts, the
  audit log, and a user's password when they accept an invite. Nothing under
  `rw()` ever touches `post_metrics`, `clients`, `client_sources`.

`visible_posts(...)` is the one place the two-day rule is applied. Every
query that shows post data to a client goes through it. `client_id` comes
from the session — never from a URL, a query string or a form.
"""
import json
import threading
import time

from . import config, schema, util

_lock = threading.Lock()
_ro = None
_rw = None


def init() -> None:
    schema.ensure_schema(config.PORTAL_DB)
    config.PORTAL_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def ro():
    global _ro
    with _lock:
        if _ro is None:
            _ro = schema.connect(config.PORTAL_DB, read_only=True)
        return _ro


def rw():
    global _rw
    with _lock:
        if _rw is None:
            _rw = schema.connect(config.PORTAL_DB)
        return _rw


def rows(sql: str, params=()) -> list:
    return [dict(r) for r in ro().execute(sql, params).fetchall()]


def one(sql: str, params=()):
    r = ro().execute(sql, params).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
def client_get(cid: str):
    c = one("SELECT * FROM clients WHERE id = ? AND archived = 0", (cid,))
    if c:
        try:
            c["category_map"] = json.loads(c.get("category_map") or "{}")
        except ValueError:
            c["category_map"] = {}
    return c


def client_by_slug(slug: str):
    c = one("SELECT id FROM clients WHERE slug = ? AND archived = 0", (slug,))
    return client_get(c["id"]) if c else None


def today_for(client: dict):
    return util.today_in(client.get("tz") or config.PORTAL_TZ)


# --------------------------------------------------------------------------- #
# The visibility rule
# --------------------------------------------------------------------------- #
VISIBLE = "client_id = ? AND visible_from <= ? AND status = 'ok'"


def visible_params(client: dict) -> tuple:
    return (client["id"], util.day_str(today_for(client)))


def visible_posts(client: dict, extra_sql: str = "", params=()) -> list:
    """Rows of post_metrics this client may see, optionally narrowed further
    (`extra_sql` starts with AND …). The visibility clause is prepended here
    and cannot be left out by a caller."""
    return rows(f"SELECT * FROM post_metrics WHERE {VISIBLE} {extra_sql}",
                visible_params(client) + tuple(params))


def data_bounds(client: dict):
    r = one(f"SELECT MIN(sheet_date) AS lo, MAX(sheet_date) AS hi FROM post_metrics WHERE {VISIBLE}",
            visible_params(client))
    return (r["lo"], r["hi"]) if r and r["lo"] else (None, None)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def audit(action: str, client_id: str = "", user_id: str = "", email: str = "",
          detail: str = "", ip: str = "") -> None:
    try:
        rw().execute("INSERT INTO portal_audit (at, client_id, user_id, email, action, detail, ip) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (time.time(), client_id, user_id, email, action, (detail or "")[:500], ip))
        rw().commit()
    except Exception as e:                      # never let logging break a request
        print(f"[portal] audit failed: {e}", flush=True)

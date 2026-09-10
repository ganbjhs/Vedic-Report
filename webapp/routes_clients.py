"""Admin → Clients: the internal side of the Client Portal.

Everything here writes `data/portal.db` through `portal_publish.connect()`.
Admin-only. The one secret handled — a scraper API key — is sealed with
PORTAL_KEY_SECRET before it is stored and is never returned by any route.
"""
import json
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from portal import secretbox
from portal import util as putil
from portal.auth import EMAIL_RE, ROLES as CLIENT_ROLES, create_user, set_login, token_hash

from . import auth, config, portal_publish, projects
from .jobs import store
from .routes_extras import _csrf, _json_body

router = APIRouter(prefix="/api/clients")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
AUTH_STYLES = ("bearer", "x-api-key", "query:api_key", "query:key", "query:token")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or "client"


def _portal_url() -> str:
    return (config.PORTAL_PUBLIC_URL or "http://127.0.0.1:8020").rstrip("/")


def _client(conn, cid: str) -> dict:
    c = portal_publish.client_get(conn, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Client not found.")
    return c


def _public_client(conn, c: dict) -> dict:
    try:
        cmap = json.loads(c.get("category_map") or "{}")
    except ValueError:
        cmap = {}
    n_posts = conn.execute("SELECT COUNT(*) AS n, MIN(sheet_date) AS lo, MAX(sheet_date) AS hi FROM post_metrics "
                           "WHERE client_id = ?", (c["id"],)).fetchone()
    return {"id": c["id"], "slug": c["slug"], "name": c["name"], "display_name": c.get("display_name") or "",
            "accent_hex": c.get("accent_hex") or "", "lag_days": c.get("lag_days"), "tz": c.get("tz"),
            "category_map": cmap, "show_screenshots": bool(c.get("show_screenshots")),
            "show_reports": bool(c.get("show_reports")), "archived": bool(c.get("archived")),
            "created_at": c.get("created_at"), "created_by": c.get("created_by") or "",
            "posts": n_posts["n"], "data_from": n_posts["lo"], "data_through": n_posts["hi"],
            "projects": [r["project_id"] for r in conn.execute(
                "SELECT project_id FROM client_projects WHERE client_id = ?", (c["id"],)).fetchall()],
            "users": [{"id": u["id"], "email": u["email"], "username": (u["username"] if "username" in u.keys() else ""),
                       "role": u["role"], "disabled": bool(u["disabled"]),
                       "has_password": bool(u["pw_hash"]), "last_login_at": u["last_login_at"],
                       "invite_pending": bool(u["invite_token"]) and (u["invite_expires"] or 0) > time.time()}
                      for u in conn.execute("SELECT * FROM client_users WHERE client_id = ? ORDER BY email",
                                            (c["id"],)).fetchall()],
            "sources": [{"id": s["id"], "signature_name": s["signature_name"], "base_url": s["base_url"],
                         "probe_url": (s["probe_url"] if "probe_url" in s.keys() else ""),
                         "auth_style": s["auth_style"], "enabled": bool(s["enabled"]), "has_key": bool(s["api_key_enc"]),
                         "added_by": s["added_by"], "added_at": s["added_at"], "last_ok_at": s["last_ok_at"],
                         "stale_hours": _stale_hours(s["last_ok_at"]),
                         "last_error": s["last_error"], "last_count": s["last_count"]}
                        for s in conn.execute("SELECT * FROM client_sources WHERE client_id = ? ORDER BY added_at",
                                              (c["id"],)).fetchall()],
            "categories": [{"raw": r["category_raw"], "posts": r["n"], "first": r["lo"]}
                           for r in conn.execute(
                               "SELECT category_raw, COUNT(*) AS n, MIN(sheet_date) AS lo FROM post_metrics "
                               "WHERE client_id = ? GROUP BY category_raw ORDER BY MIN(id)", (c["id"],)).fetchall()],
            "log": [dict(r) for r in conn.execute(
                "SELECT * FROM publish_log WHERE client_id = ? ORDER BY at DESC LIMIT 40", (c["id"],)).fetchall()],
            "audit": [dict(r) for r in conn.execute(
                "SELECT at, email, action, detail, ip FROM portal_audit WHERE client_id = ? ORDER BY at DESC LIMIT 30",
                (c["id"],)).fetchall()]}


def _stale_hours(last_ok_at):
    """Hours since this source last answered, or None if it never has. A
    scheduled pull that fails is otherwise SILENT — it lands in `last_error`
    and the container log and nobody is told — so the age of the last success
    is the one number that says whether the integration is actually running."""
    if not last_ok_at:
        return None
    return round(max(0.0, time.time() - float(last_ok_at)) / 3600.0, 1)


def _list(conn) -> list:
    return [{"id": r["id"], "slug": r["slug"], "name": r["name"], "archived": bool(r["archived"]),
             "posts": conn.execute("SELECT COUNT(*) FROM post_metrics WHERE client_id = ?", (r["id"],)).fetchone()[0],
             "users": conn.execute("SELECT COUNT(*) FROM client_users WHERE client_id = ? AND disabled = 0",
                                   (r["id"],)).fetchone()[0],
             "projects": conn.execute("SELECT COUNT(*) FROM client_projects WHERE client_id = ?",
                                      (r["id"],)).fetchone()[0]}
            for r in conn.execute("SELECT * FROM clients ORDER BY archived, name").fetchall()]


@router.get("")
async def list_clients(user: str = Depends(auth.require_admin)):
    conn = portal_publish.connect()
    try:
        return {"clients": _list(conn), "portal_url": _portal_url(),
                "key_secret_set": bool(portal_publish.KEY_SECRET)}
    finally:
        conn.close()


@router.post("")
async def create_client(request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    name = str(data.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Give the client a name.")
    slug = str(data.get("slug") or "").strip().lower() or _slugify(name)
    if not _SLUG.match(slug):
        raise HTTPException(status_code=400, detail="Slug: 2–41 chars, lowercase letters, digits and dashes.")
    conn = portal_publish.connect()
    try:
        if conn.execute("SELECT 1 FROM clients WHERE slug = ?", (slug,)).fetchone():
            raise HTTPException(status_code=400, detail="That slug is taken.")
        cid = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO clients (id, slug, name, display_name, accent_hex, lag_days, tz, category_map, "
                     "show_screenshots, show_reports, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, slug, name, str(data.get("display_name") or name)[:80],
                      _hex(data.get("accent_hex")), _lag(data.get("lag_days")),
                      str(data.get("tz") or "Asia/Kolkata")[:40], "{}",
                      1 if data.get("show_screenshots") else 0, 1 if data.get("show_reports") else 0,
                      user, time.time()))
        conn.commit()
        return {"ok": True, "client": _public_client(conn, _client(conn, cid)), "clients": _list(conn)}
    finally:
        conn.close()


def _hex(v) -> str:
    v = str(v or "").strip()
    return v if _HEX.match(v) else ""


def _lag(v) -> int:
    try:
        return max(0, min(30, int(v if v not in (None, "") else 2)))
    except (TypeError, ValueError):
        return 2


@router.get("/{cid}")
async def get_client(cid: str, user: str = Depends(auth.require_admin)):
    conn = portal_publish.connect()
    try:
        return {"client": _public_client(conn, _client(conn, cid)), "portal_url": _portal_url(),
                "key_secret_set": bool(portal_publish.KEY_SECRET)}
    finally:
        conn.close()


@router.patch("/{cid}")
async def update_client(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    conn = portal_publish.connect()
    try:
        c = _client(conn, cid)
        sets, vals = [], []
        if "name" in data and str(data["name"]).strip():
            sets.append("name = ?"); vals.append(str(data["name"]).strip()[:80])
        if "display_name" in data:
            sets.append("display_name = ?"); vals.append(str(data["display_name"] or "").strip()[:80])
        if "accent_hex" in data:
            sets.append("accent_hex = ?"); vals.append(_hex(data["accent_hex"]))
        if "tz" in data:
            sets.append("tz = ?"); vals.append(str(data["tz"] or "Asia/Kolkata")[:40])
        if "show_screenshots" in data:
            sets.append("show_screenshots = ?"); vals.append(1 if data["show_screenshots"] else 0)
        if "show_reports" in data:
            sets.append("show_reports = ?"); vals.append(1 if data["show_reports"] else 0)
        if "archived" in data:
            sets.append("archived = ?"); vals.append(1 if data["archived"] else 0)
        if "category_map" in data and isinstance(data["category_map"], dict):
            clean = {}
            for raw, m in data["category_map"].items():
                if not isinstance(m, dict):
                    continue
                entry = {"label": str(m.get("label") or "")[:80], "hidden": bool(m.get("hidden"))}
                try:
                    entry["order"] = int(m.get("order")) if m.get("order") not in (None, "") else ""
                except (TypeError, ValueError):
                    entry["order"] = ""
                clean[str(raw)[:120]] = entry
            sets.append("category_map = ?"); vals.append(json.dumps(clean, ensure_ascii=False))
        lag_changed = False
        if "lag_days" in data:
            lag = _lag(data["lag_days"])
            if lag != int(c.get("lag_days") or 2):
                lag_changed = True
            sets.append("lag_days = ?"); vals.append(lag)
        if sets:
            vals.append(cid)
            conn.execute(f"UPDATE clients SET {', '.join(sets)} WHERE id = ?", vals)
            if lag_changed:
                # re-derive every row's visible_from so the new delay applies everywhere
                conn.execute("UPDATE post_metrics SET visible_from = date(sheet_date, ?) WHERE client_id = ?",
                             (f"+{_lag(data['lag_days'])} days", cid))
            conn.commit()
        return {"ok": True, "client": _public_client(conn, _client(conn, cid)), "clients": _list(conn)}
    finally:
        conn.close()


@router.post("/{cid}/projects")
async def set_projects(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    ids = [str(x) for x in (data.get("project_ids") or []) if store.project_get(str(x))]
    conn = portal_publish.connect()
    try:
        _client(conn, cid)
        before = {r["project_id"] for r in conn.execute(
            "SELECT project_id FROM client_projects WHERE client_id = ?", (cid,)).fetchall()}
        conn.execute("DELETE FROM client_projects WHERE client_id = ?", (cid,))
        for pid in ids:
            conn.execute("INSERT INTO client_projects (client_id, project_id, linked_by, linked_at) VALUES (?,?,?,?)",
                         (cid, pid, user, time.time()))
        conn.commit()
    finally:
        conn.close()
    new = [p for p in ids if p not in before]
    result = {"ok": True, "backfilled": None}
    if new and data.get("backfill", True):
        result["backfilled"] = portal_publish.backfill_client(cid, by_user=user)
    conn = portal_publish.connect()
    try:
        result["client"] = _public_client(conn, _client(conn, cid))
        result["clients"] = _list(conn)
        return result
    finally:
        conn.close()


@router.post("/{cid}/backfill")
async def backfill(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    r = portal_publish.backfill_client(cid, by_user=user)
    conn = portal_publish.connect()
    try:
        return {"ok": True, "result": r, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


# ---- users ------------------------------------------------------------------
@router.post("/{cid}/users")
async def invite_user(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    email = str(data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="That is not an e-mail address.")
    role = str(data.get("role") or "viewer")
    if role not in CLIENT_ROLES:
        raise HTTPException(status_code=400, detail="Role must be viewer or manager.")
    conn = portal_publish.connect()
    try:
        _client(conn, cid)
        made = create_user(conn, cid, email, role, invited_by=user)
        return {"ok": True, "invite_url": f"{_portal_url()}/invite/{made['token']}",
                "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.post("/{cid}/users/set-credentials")
async def set_credentials(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    """Staff assign a client's e-mail, optional username and password directly
    (no invite link). Creates the user or resets an existing one."""
    data = await _json_body(request)
    _csrf(request, data)
    conn = portal_publish.connect()
    try:
        _client(conn, cid)
        try:
            made = set_login(conn, cid, str(data.get("email") or ""), str(data.get("password") or ""),
                             username=str(data.get("username") or ""), role=str(data.get("role") or "viewer"),
                             by=user)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "user": made, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.post("/{cid}/users/{uid}/reinvite")
async def reinvite(cid: str, uid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    conn = portal_publish.connect()
    try:
        u = conn.execute("SELECT * FROM client_users WHERE id = ? AND client_id = ?", (uid, cid)).fetchone()
        if not u:
            raise HTTPException(status_code=404, detail="User not found.")
        made = create_user(conn, cid, u["email"], u["role"], invited_by=user)
        conn.execute("DELETE FROM client_sessions WHERE user_id = ?", (uid,))
        conn.commit()
        return {"ok": True, "invite_url": f"{_portal_url()}/invite/{made['token']}",
                "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.patch("/{cid}/users/{uid}")
async def update_user(cid: str, uid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    conn = portal_publish.connect()
    try:
        u = conn.execute("SELECT * FROM client_users WHERE id = ? AND client_id = ?", (uid, cid)).fetchone()
        if not u:
            raise HTTPException(status_code=404, detail="User not found.")
        if "disabled" in data:
            conn.execute("UPDATE client_users SET disabled = ? WHERE id = ?", (1 if data["disabled"] else 0, uid))
            if data["disabled"]:
                conn.execute("DELETE FROM client_sessions WHERE user_id = ?", (uid,))
        if "role" in data and data["role"] in CLIENT_ROLES:
            conn.execute("UPDATE client_users SET role = ? WHERE id = ?", (data["role"], uid))
        conn.commit()
        return {"ok": True, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


# ---- data source ------------------------------------------------------------
@router.post("/{cid}/sources")
async def add_source(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    sig = str(data.get("signature_name") or "").strip()[:60]
    url = str(data.get("base_url") or "").strip()
    key = str(data.get("api_key") or "")
    style = str(data.get("auth_style") or "bearer")
    probe = str(data.get("probe_url") or "").strip()
    if probe and not probe.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The handshake URL must start with http:// or https://")
    if not sig:
        raise HTTPException(status_code=400, detail="Give the key a signature name.")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="The API URL must start with http:// or https://")
    if style not in AUTH_STYLES:
        raise HTTPException(status_code=400, detail="Unknown auth style.")
    if key and not portal_publish.KEY_SECRET:
        raise HTTPException(status_code=400, detail="PORTAL_KEY_SECRET is not set in .env — set it (and PORTAL_SESSION_SECRET) "
                                                    "before saving an API key, so it can be stored sealed.")
    conn = portal_publish.connect()
    try:
        _client(conn, cid)
        sid = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO client_sources (id, client_id, signature_name, base_url, api_key_enc, auth_style, "
                     "probe_url, enabled, added_by, added_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
                     (sid, cid, sig, url, secretbox.seal(portal_publish.KEY_SECRET, key) if key else "", style,
                      probe, user, time.time()))
        conn.commit()
        return {"ok": True, "source_id": sid, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.patch("/{cid}/sources/{sid}")
async def update_source(cid: str, sid: str, request: Request, user: str = Depends(auth.require_admin)):
    """Change a source without re-typing its key. Only the fields that are safe
    to edit; the sealed key is replaced only when a new one is actually sent."""
    data = await _json_body(request)
    _csrf(request, data)
    conn = portal_publish.connect()
    try:
        row = conn.execute("SELECT id FROM client_sources WHERE id = ? AND client_id = ?", (sid, cid)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Source not found.")
        sets, vals = [], []
        if "probe_url" in data:
            probe = str(data["probe_url"] or "").strip()
            if probe and not probe.lower().startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="The handshake URL must start with http:// or https://")
            sets.append("probe_url = ?"); vals.append(probe)
        if "base_url" in data and str(data["base_url"]).strip():
            u = str(data["base_url"]).strip()
            if not u.lower().startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="The API URL must start with http:// or https://")
            sets.append("base_url = ?"); vals.append(u)
        if "auth_style" in data:
            if data["auth_style"] not in AUTH_STYLES:
                raise HTTPException(status_code=400, detail="Unknown auth style.")
            sets.append("auth_style = ?"); vals.append(data["auth_style"])
        if "enabled" in data:
            sets.append("enabled = ?"); vals.append(1 if data["enabled"] else 0)
        if data.get("api_key"):
            if not portal_publish.KEY_SECRET:
                raise HTTPException(status_code=400, detail="PORTAL_KEY_SECRET is not set in .env, so a key cannot be stored sealed.")
            sets.append("api_key_enc = ?")
            vals.append(secretbox.seal(portal_publish.KEY_SECRET, str(data["api_key"])))
        if sets:
            vals.append(sid)
            conn.execute(f"UPDATE client_sources SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
        return {"ok": True, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.post("/{cid}/sources/{sid}/test")
async def test_source(cid: str, sid: str, request: Request, user: str = Depends(auth.require_admin)):
    """The handshake. Answers 'which project and which sheet is this key
    actually wired to' BEFORE a sync files one client's posts under another —
    the failure a project-locked key exists to prevent, and the one an HTTP 401
    hours later does not explain."""
    data = await _json_body(request)
    _csrf(request, data)
    import asyncio

    from portal import scraper as pscraper
    conn = portal_publish.connect()
    try:
        s = conn.execute("SELECT * FROM client_sources WHERE id = ? AND client_id = ?", (sid, cid)).fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="Source not found.")
        src = dict(s)
        if not (src.get("probe_url") or "").strip():
            raise HTTPException(status_code=400, detail="This source has no handshake URL yet. Add one — for the "
                                                        "Collector it is the /api/project address for the same project.")
        if not portal_publish.KEY_SECRET:
            raise HTTPException(status_code=400, detail="PORTAL_KEY_SECRET is not set in .env, so the saved key cannot be opened.")
        try:
            key = secretbox.open_(portal_publish.KEY_SECRET, src.get("api_key_enc") or "") if src.get("api_key_enc") else ""
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"The saved key could not be opened: {e}")
    finally:
        conn.close()
    try:
        info = await asyncio.to_thread(pscraper.probe, src, key)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    proj, wl = info.get("project") or {}, info.get("watchlist") or {}
    return {"ok": True, "summary": {
        "project_id": proj.get("id"), "project_name": proj.get("name"),
        "sheet_title": wl.get("sheet_title"), "sheet_url": wl.get("sheet_url"),
        "tab_mode": wl.get("tab_mode"), "tabs": wl.get("tabs"), "dated_tabs": wl.get("dated_tabs"),
        "links": wl.get("links"), "sheet_error": wl.get("sheet_error") or "",
        "counters": info.get("counters") or {}, "skipped_non_x": info.get("skipped_non_x"),
        "refresh_in_progress": bool(info.get("refresh_in_progress")),
        "last_refresh_ms": info.get("last_refresh_ms"),
    }}


@router.delete("/{cid}/sources/{sid}")
async def delete_source(cid: str, sid: str, request: Request, user: str = Depends(auth.require_admin)):
    auth.verify_csrf(request, request.headers.get("x-csrf-token") or "")
    conn = portal_publish.connect()
    try:
        conn.execute("DELETE FROM client_sources WHERE id = ? AND client_id = ?", (sid, cid))
        conn.commit()
        return {"ok": True, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.post("/{cid}/sync")
async def sync_now(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    import asyncio
    r = await asyncio.to_thread(portal_publish.sync_client, cid, str(data.get("from") or ""),
                                str(data.get("to") or ""), user, str(data.get("source_id") or ""))
    conn = portal_publish.connect()
    try:
        return {"ok": not r.get("errors"), "result": r, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()


@router.post("/{cid}/sync-sheet")
async def sync_sheet_now(cid: str, request: Request, user: str = Depends(auth.require_admin)):
    """Read the client's dashboard sheets right now, no capture run."""
    data = await _json_body(request)
    _csrf(request, data)
    import asyncio
    r = await asyncio.to_thread(portal_publish.publish_from_sheet, cid, user)
    conn = portal_publish.connect()
    try:
        return {"ok": not r.get("errors"), "result": r, "client": _public_client(conn, _client(conn, cid))}
    finally:
        conn.close()

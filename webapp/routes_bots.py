"""Admin → Bots: the registry behind `/v1`.

A bot is a token plus the scopes it may use, the projects it may touch and the
people it may act for. Everything here is admin-only: "who may send messages on
behalf of the team" is a question that should have exactly one answer in
exactly one place, and this is it.

The raw token is shown once, on creation or regeneration, and never again —
only its sha256 and its last four characters are stored. Regenerate is both
rotation and revocation; there is no separate delete-the-key button because
there is no key to delete.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import auth, config
from .jobs import store

router = APIRouter(prefix="/api/bots")


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


def public_bot(bot: dict) -> dict:
    """What the page may see. The token hash never leaves the server."""
    scopes = sorted(store.bot_scopes_of(bot["id"]))
    names = {p["id"]: p["name"] for p in store.projects_list(include_archived=True)}
    return {
        "id": bot["id"], "name": bot["name"], "kind": bot.get("kind") or "telegram",
        "status": bot.get("status") or "active",
        "test": bool(bot.get("is_test")),
        "hint": bot.get("token_hint") or "",
        "scopes": scopes,
        "preset": _preset_name(scopes),
        "projects": bot.get("projects") or [],
        "project_names": [names.get(p, p) for p in (bot.get("projects") or [])],
        "audience": bot.get("audience") or [],
        "limits": bot.get("limits") or {},
        "created_by": bot.get("created_by") or "",
        "created_at": bot.get("created_at"),
        "last_used_at": bot.get("last_used_at"),
        "calls_last_hour": store.api_calls_since(bot["id"], 3600),
        "features": store.bot_features_list(bot["id"]),
    }


def _preset_name(scopes) -> str:
    """Which preset this set of scopes IS, if any — so the page can say
    'Operator' instead of listing six slugs, without storing a label that could
    drift out of step with the scopes it claims to describe."""
    have = set(scopes)
    for name, preset in store.SCOPE_PRESETS.items():
        if have == set(preset):
            return name
    return "custom"


def _scopes_from(data: dict) -> list:
    """Either a preset name or an explicit list. A preset is expanded here, so
    the stored rows are always the real scopes and a changed preset never
    silently re-grants an existing bot something new."""
    preset = str(data.get("preset") or "").strip().lower()
    if preset and preset in store.SCOPE_PRESETS:
        return list(store.SCOPE_PRESETS[preset])
    asked = [str(s).strip() for s in (data.get("scopes") or []) if str(s).strip()]
    unknown = [s for s in asked if s not in store.SCOPES]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Unknown scope(s): {', '.join(unknown)}.")
    return asked


def _limits_from(data: dict) -> dict:
    raw = data.get("limits") or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in store.DEFAULT_LIMITS:
        if key in raw:
            try:
                out[key] = max(0, int(raw[key]))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,
                                    detail=f"{key} must be a whole number.")
    return out


def _known_projects(ids) -> list:
    known = {p["id"] for p in store.projects_list(include_archived=True)}
    out = [str(p) for p in (ids or []) if str(p) in known]
    missing = [str(p) for p in (ids or []) if str(p) not in known]
    if missing:
        raise HTTPException(status_code=404,
                            detail="No such project: " + ", ".join(missing))
    return out


# --------------------------------------------------------------------------- #
# Bots
# --------------------------------------------------------------------------- #
@router.get("")
async def list_bots(user: str = Depends(auth.require_admin)):
    return {"bots": [public_bot(b) for b in store.bots_list()],
            "scopes": list(store.SCOPES),
            "presets": {k: list(v) for k, v in store.SCOPE_PRESETS.items()},
            "defaults": dict(store.DEFAULT_LIMITS)}


@router.post("")
async def create_bot(request: Request, user: str = Depends(auth.require_admin)):
    """Create a bot. The response carries the raw token — the ONLY time it
    exists outside the caller's hands."""
    data = await _json_body(request)
    _csrf(request, data)
    name = str(data.get("name") or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Give the bot a name.")
    scopes = _scopes_from(data)
    if not scopes:
        raise HTTPException(status_code=400,
                            detail="A bot with no scopes can do nothing. Pick a "
                                   "preset or tick at least one scope.")
    bot, token = store.bot_create(
        name=name,
        kind=str(data.get("kind") or "telegram"),
        scopes=scopes,
        projects=_known_projects(data.get("projects")),
        audience=[str(a).strip() for a in (data.get("audience") or []) if str(a).strip()],
        limits=_limits_from(data),
        is_test=bool(data.get("test")),
        created_by=user)
    return {"bot": public_bot(bot), "token": token}


@router.patch("/{bot_id}")
async def update_bot(bot_id: str, request: Request,
                     user: str = Depends(auth.require_admin)):
    data = await _json_body(request)
    _csrf(request, data)
    if store.bot_get(bot_id) is None:
        raise HTTPException(status_code=404, detail="No such bot.")

    fields = {}
    if "name" in data:
        name = str(data["name"]).strip()
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="Give the bot a name.")
        fields["name"] = name
    if "status" in data:
        status = str(data["status"]).strip()
        if status not in ("active", "disabled"):
            raise HTTPException(status_code=400,
                                detail="Status is 'active' or 'disabled'.")
        fields["status"] = status
    if "projects" in data:
        fields["projects"] = _known_projects(data.get("projects"))
    if "audience" in data:
        fields["audience"] = [str(a).strip() for a in (data.get("audience") or [])
                              if str(a).strip()]
    if "limits" in data:
        fields["limits"] = _limits_from(data)
    if fields:
        store.bot_update(bot_id, **fields)

    if "scopes" in data or "preset" in data:
        scopes = _scopes_from(data)
        if not scopes:
            raise HTTPException(status_code=400,
                                detail="A bot with no scopes can do nothing.")
        store.bot_set_scopes(bot_id, scopes)
    return {"bot": public_bot(store.bot_get(bot_id))}


@router.post("/{bot_id}/regenerate")
async def regenerate(bot_id: str, request: Request,
                     user: str = Depends(auth.require_admin)):
    """Rotate the token. The old one stops working the instant this returns —
    revocation and rotation are the same button, pressed for different
    reasons."""
    data = await _json_body(request)
    _csrf(request, data)
    bot, token = store.bot_regenerate(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="No such bot.")
    return {"bot": public_bot(bot), "token": token}


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str, request: Request,
                     user: str = Depends(auth.require_admin)):
    _csrf(request, {})
    if not store.bot_delete(bot_id):
        raise HTTPException(status_code=404, detail="No such bot.")
    # The audit rows stay: deleting a bot must not delete the record of what it
    # did, which is the one thing the log is for.
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Identities — which colleague a Telegram id acts as
# --------------------------------------------------------------------------- #
@router.get("/identities/all")
async def list_identities(user: str = Depends(auth.require_admin)):
    return {"identities": store.identities_list()}


@router.post("/identities/code")
async def make_link_code(request: Request, user: str = Depends(auth.require_admin)):
    """A one-shot code the colleague sends to the bot as `/link 482913`. Valid
    for 30 minutes, spent on use — so a code seen over someone's shoulder is
    worth nothing a moment later."""
    data = await _json_body(request)
    _csrf(request, data)
    username = str(data.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Which user is this for?")
    if not (store.user_get(username) or username in config.USERS):
        raise HTTPException(status_code=404, detail=f"No user {username!r}.")
    return {"code": store.link_code_create(username, created_by=user),
            "user": username, "minutes": 30}


@router.delete("/identities/{actor}")
async def unlink(actor: str, request: Request,
                 user: str = Depends(auth.require_admin)):
    _csrf(request, {})
    if not store.identity_delete(actor):
        raise HTTPException(status_code=404, detail="Not linked.")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
@router.get("/{bot_id}/features")
async def list_features(bot_id: str, user: str = Depends(auth.require_admin)):
    if store.bot_get(bot_id) is None:
        raise HTTPException(status_code=404, detail="No such bot.")
    return {"features": store.bot_features_list(bot_id)}


@router.post("/{bot_id}/features/{name}")
async def set_feature(bot_id: str, name: str, request: Request,
                      user: str = Depends(auth.require_admin)):
    """`{"on": true|false|null}` — null hands the decision back to the bot's
    own default, which is different from switching it off and worth being able
    to say."""
    data = await _json_body(request)
    _csrf(request, data)
    on = data.get("on", None)
    if on is not None and not isinstance(on, bool):
        raise HTTPException(status_code=400, detail="'on' is true, false or null.")
    if not store.bot_feature_set(bot_id, name, on):
        raise HTTPException(status_code=404,
                            detail=f"This bot has not announced a feature "
                                   f"called {name!r}. It announces its list "
                                   "when it starts — has it connected yet?")
    return {"features": store.bot_features_list(bot_id)}


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
@router.get("/calls/recent")
async def recent_calls(limit: int = 100, bot: str = "",
                       user: str = Depends(auth.require_admin)):
    return {"calls": store.api_calls_recent(min(max(limit, 1), 500), bot)}

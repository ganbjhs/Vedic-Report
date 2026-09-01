"""`/v1` — the scoped token API, and the one place the permission rule lives.

    effective permission  =  the bot token's scopes  n  the acting person's role

A bot can never exceed its token; a person can never exceed their own account.
That is the whole safety story, and it is evaluated in `effective_scopes()`
below rather than sprinkled through the endpoints.

Two headers matter:

* `Authorization: Bearer vr_...` — WHICH bot. Registered in Admin → Bots,
  stored hashed, revoked by regenerating.
* `X-Actor: tg:<id>` — WHICH colleague is acting behind it. Resolved through
  `tg_identities` to a Report Maker account, whose role is the ceiling. An
  unlinked actor is a stranger and gets the empty set.

Every call rows into `api_calls`, refusals included: a log that only records
successes cannot answer the question it exists for.

The browser's own `/api` endpoints are untouched. This runs beside them and
reuses the same `runs.create_run` and `uploads.analyse`, so a report made from
a token is the same report made from the New run page — it shows in Runs, it
belongs to the project, and it is built by the same code.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse, JSONResponse

from . import config, projects, report_types, runs, uploads
from .jobs import runner, store
from .routes_jobs import _KINDS, _grid_from_request, public_job

router = APIRouter(prefix="/v1")

# A caller sends links and gets that project's report. Style, platform and
# options live in the project, not in the request — that is what keeps an
# integration one call wide (docs/v3-plan.md §11).
MAX_LINKS_PER_CALL = 200


class Caller:
    """One authenticated (bot, actor) pair, with the intersection resolved."""

    def __init__(self, bot: dict, actor: str, username: str, role: str,
                 scopes: set):
        self.bot = bot
        self.actor = actor
        self.username = username
        self.role = role
        self.scopes = scopes

    @property
    def is_test(self) -> bool:
        return bool(self.bot.get("is_test"))

    def may(self, scope: str) -> bool:
        return scope in self.scopes

    def audit(self, request: Request, scope: str, status: int,
              detail: str = "", project_id: str = "", job_id: str = "") -> None:
        store.api_call_record(
            bot_id=self.bot["id"], bot_name=self.bot.get("name") or "",
            actor=self.actor, username=self.username, scope=scope,
            method=request.method, path=request.url.path,
            project_id=project_id, job_id=job_id, status=status, detail=detail)

    def require(self, request: Request, scope: str, project_id: str = "") -> None:
        """Refuse, and say WHICH half of the intersection refused — "the bot may
        not" and "you may not" are different problems with different fixes, and
        a caller who cannot tell them apart asks the wrong person."""
        if self.may(scope):
            return
        if scope not in self.bot.get("_token_scopes", set()):
            detail = (f"This bot does not have the {scope!r} scope. An "
                      "administrator can grant it in Admin → Bots.")
        elif not self.username:
            detail = ("Your Telegram account is not linked to a Report Maker "
                      "user yet, so nothing can be done on your behalf. Ask an "
                      "administrator for a link code.")
        else:
            detail = (f"Your role ({self.role}) does not allow {scope!r}, so "
                      "the bot cannot do it for you either.")
        self.audit(request, scope, 403, detail, project_id=project_id)
        raise HTTPException(status_code=403, detail=detail)


def effective_scopes(bot: dict, username: str, role: str) -> set:
    """The intersection. An actor with no linked account gets the EMPTY set —
    not the token's scopes, which is the failure this header exists to
    prevent."""
    token = store.bot_scopes_of(bot["id"])
    if not username:
        return set()
    return token & store.ROLE_CEILING.get(role, set())


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


async def require_token(request: Request) -> Caller:
    """FastAPI dependency: authenticate the bot, resolve the actor, intersect.

    Deliberately does NOT look at the session cookie. A token call is not a
    browser call, and letting a signed-in session stand in for a token would
    make the audit log lie about who did what.
    """
    from . import auth

    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401,
                            detail="Send Authorization: Bearer vr_…")
    bot = store.bot_by_token(token)
    if not bot:
        store.api_call_record(method=request.method, path=request.url.path,
                              status=401, detail="unknown token")
        raise HTTPException(status_code=401, detail="Unknown or revoked token.")
    if bot.get("status") != "active":
        store.api_call_record(bot_id=bot["id"], bot_name=bot["name"],
                              method=request.method, path=request.url.path,
                              status=403, detail="bot disabled")
        raise HTTPException(status_code=403,
                            detail=f"The bot {bot['name']!r} is disabled.")

    actor = (request.headers.get("x-actor") or "").strip()[:64]

    # The audience list, when set, is the outer gate: a token that leaks is
    # still useless to somebody who is not on it.
    audience = bot.get("audience") or []
    if audience and actor not in audience:
        detail = ("This bot only acts for the people it was registered for."
                  if actor else
                  "This bot requires an X-Actor header naming who is acting.")
        store.api_call_record(bot_id=bot["id"], bot_name=bot["name"], actor=actor,
                              method=request.method, path=request.url.path,
                              status=403, detail=detail)
        raise HTTPException(status_code=403, detail=detail)

    identity = store.identity_get(actor) if actor else None
    username = identity["username"] if identity else ""
    if username and not (store.user_get(username) or config.USERS.get(username)):
        # The account was deleted after the identity was linked. Fail closed.
        username = ""
    role = auth.role_of(username) if username else ""
    if identity:
        store.identity_seen(actor)
    store.bot_touch(bot["id"])

    bot["_token_scopes"] = store.bot_scopes_of(bot["id"])
    return Caller(bot, actor, username, role,
                  effective_scopes(bot, username, role))


# --------------------------------------------------------------------------- #
# Project access + limits
# --------------------------------------------------------------------------- #
def _project_for(caller: Caller, request: Request, project_id: str) -> dict:
    """The project this call is about, refusing anything outside the bot's list.

    An empty list on the bot means every project — the one-key-per-project
    shape of §11 is then just a bot whose list has exactly one entry, and that
    bot needs no `project_id` in the request at all.
    """
    allowed = caller.bot.get("projects") or []
    project = store.project_get(project_id) if project_id else None
    if project is None and not project_id and len(allowed) == 1:
        project = store.project_get(allowed[0])
    if project is None:
        caller.audit(request, "project.read", 404, "project not found")
        raise HTTPException(status_code=404, detail="Project not found.")
    if allowed and project["id"] not in allowed:
        detail = f"This bot may not touch {project['name']!r}."
        caller.audit(request, "project.read", 403, detail)
        raise HTTPException(status_code=403, detail=detail)
    return project


def _check_rate(caller: Caller, request: Request) -> None:
    """Counted from the audit log rather than from memory, so a restart cannot
    hand somebody a fresh allowance."""
    per_hour = int((caller.bot.get("limits") or {}).get("runs_per_hour") or 0)
    if per_hour and store.api_calls_since(caller.bot["id"], 3600,
                                          "report.run") >= per_hour:
        detail = (f"This bot's limit of {per_hour} runs an hour is used up. "
                  "It frees up on a rolling hour.")
        caller.audit(request, "report.run", 429, detail)
        raise HTTPException(status_code=429, detail=detail)


async def _body(request: Request) -> dict:
    try:
        return await request.json() if await request.body() else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="Send a JSON body.")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
@router.get("/whoami")
async def whoami(request: Request, caller: Caller = Depends(require_token)):
    """What this token and this actor can actually do — the first call any
    integration should make, because it answers "why was that refused" before
    it happens."""
    caller.audit(request, "", 200)
    return {
        "bot": {"id": caller.bot["id"], "name": caller.bot["name"],
                "kind": caller.bot.get("kind"), "test": caller.is_test,
                "projects": caller.bot.get("projects") or [],
                "limits": caller.bot.get("limits") or {}},
        "actor": caller.actor,
        "user": caller.username or None,
        "role": caller.role or None,
        "token_scopes": sorted(caller.bot.get("_token_scopes", set())),
        "scopes": sorted(caller.scopes),
        "linked": bool(caller.username),
    }


@router.post("/link")
async def link_actor(request: Request, caller: Caller = Depends(require_token)):
    """Bind the acting Telegram id to a Report Maker account with a one-shot
    code an admin generated. The bot relays `/link 482913`; it never sees a
    password, and the code dies on use."""
    body = await _body(request)
    code = str((body or {}).get("code") or "").strip()
    if not caller.actor:
        raise HTTPException(status_code=400, detail="No X-Actor header to link.")
    if "users.link" not in caller.bot.get("_token_scopes", set()):
        detail = "This bot does not have the 'users.link' scope."
        caller.audit(request, "users.link", 403, detail)
        raise HTTPException(status_code=403, detail=detail)
    username = store.link_code_consume(code, caller.actor)
    if not username:
        detail = "That code is wrong, already used, or more than 30 minutes old."
        caller.audit(request, "users.link", 400, detail)
        raise HTTPException(status_code=400, detail=detail)
    caller.audit(request, "users.link", 200, f"linked to {username}")
    return {"ok": True, "user": username}


# --------------------------------------------------------------------------- #
# Features — the bot describes itself, an admin decides
#
# Authenticated by the TOKEN ALONE, with no actor and no scope check. That is
# deliberate and worth stating: a bot announces its catalogue and reads its
# switches at start-up, before anybody has pressed a button, so requiring a
# linked actor would mean a bot could never boot until somebody used it. There
# is nothing here to abuse either — a token may describe only itself, and may
# only read back what an administrator already decided.
# --------------------------------------------------------------------------- #
@router.post("/features")
async def announce_features(request: Request,
                            caller: Caller = Depends(require_token)):
    """`{"features": [{"name","label","why","default"}, ...]}` in, the
    effective `{name: on}` out. Idempotent: safe to call on every start."""
    body = await _body(request)
    catalogue = body.get("features")
    if not isinstance(catalogue, list):
        raise HTTPException(status_code=400, detail="Send a 'features' array.")
    if len(catalogue) > 100:
        raise HTTPException(status_code=400, detail="At most 100 features.")
    store.bot_features_announce(caller.bot["id"], catalogue)
    caller.audit(request, "", 200, f"announced {len(catalogue)} features")
    return {"features": store.bot_features_effective(caller.bot["id"])}


@router.get("/features")
async def read_features(request: Request,
                        caller: Caller = Depends(require_token)):
    """What is switched on for this bot right now. Polled, so it is cheap and
    says nothing an administrator has not already set."""
    return {"features": store.bot_features_effective(caller.bot["id"])}


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
@router.get("/projects")
async def list_projects(request: Request, caller: Caller = Depends(require_token)):
    caller.require(request, "project.read")
    allowed = caller.bot.get("projects") or []
    out = [projects.public(p) for p in store.projects_list()
           if not allowed or p["id"] in allowed]
    caller.audit(request, "project.read", 200)
    return {"projects": out}


@router.get("/projects/{pid}")
async def get_project(pid: str, request: Request,
                      caller: Caller = Depends(require_token)):
    caller.require(request, "project.read")
    project = _project_for(caller, request, pid)
    caller.audit(request, "project.read", 200, project_id=project["id"])
    return projects.public(project)


# --------------------------------------------------------------------------- #
# Preview + run
# --------------------------------------------------------------------------- #
def _rows_from(links: list, platform: str, dedupe: bool = True) -> dict:
    grid = uploads.grid_from_text("\n".join(str(u) for u in links))
    return uploads.analyse(grid, dedupe, platform)


def _style_platform(project: dict, styles: list) -> str:
    """The platform comes from the STYLE, never guessed from the links — the
    pairing the server enforces everywhere else."""
    picked = [s for s in projects.styles_of(project)
              if not s["missing"] and (not styles or s["slug"] in styles)]
    if not picked:
        return report_types.DEFAULT_PLATFORM
    kinds = {s["platform"] for s in picked}
    return "combined" if len(kinds) > 1 else picked[0]["platform"]


@router.post("/preview")
async def preview(request: Request, caller: Caller = Depends(require_token)):
    """What WOULD be captured, costing nothing. Same analyser as the browser."""
    caller.require(request, "report.preview")
    body = await _body(request)
    project = _project_for(caller, request, str(body.get("project_id") or ""))
    links = [str(x) for x in (body.get("links") or []) if str(x).strip()]
    if not links:
        raise HTTPException(status_code=400, detail="Send a 'links' array.")
    styles = [str(s) for s in (body.get("styles") or []) if str(s).strip()]
    platform = _style_platform(project, styles)
    try:
        report = await asyncio.to_thread(_rows_from, links, platform,
                                         bool(body.get("dedupe", True)))
    except uploads.UploadError as e:
        caller.audit(request, "report.preview", 400, str(e), project["id"])
        raise HTTPException(status_code=400, detail=str(e))
    caller.audit(request, "report.preview", 200, project_id=project["id"])
    return {"project_id": project["id"], "platform": platform,
            "count": len(report["rows"]),
            "duplicate_count": report["duplicate_count"],
            "dropped_count": len(report["dropped"]),
            "dropped": report["dropped"][:50]}


@router.post("/run")
async def start_run(request: Request, caller: Caller = Depends(require_token)):
    """`{"links": [...]}` in, `{run_id, status}` out.

    Everything else — which styles, which formats, which platform — comes from
    the project. That is what keeps the integration one call wide, and what
    makes revoking a client's access one button.
    """
    caller.require(request, "report.run")
    _check_rate(caller, request)
    body = await _body(request)
    project = _project_for(caller, request, str(body.get("project_id") or ""))
    links = [str(x) for x in (body.get("links") or []) if str(x).strip()]
    if not links:
        raise HTTPException(status_code=400, detail="Send a 'links' array.")

    cap = int((caller.bot.get("limits") or {}).get("links_per_call")
              or MAX_LINKS_PER_CALL)
    if len(links) > cap:
        detail = f"{len(links)} links — this bot's limit is {cap} per call."
        caller.audit(request, "report.run", 400, detail, project["id"])
        raise HTTPException(status_code=400, detail=detail)

    styles = [str(s) for s in (body.get("styles") or []) if str(s).strip()]
    outputs = [str(o).strip().lower() for o in (body.get("outputs") or [])
               if str(o).strip()]
    name = str(body.get("name") or "").strip() or time.strftime("Report %d-%m-%y %H:%M")
    platform = _style_platform(project, styles)

    try:
        report = await asyncio.to_thread(_rows_from, links, platform,
                                         bool(body.get("dedupe", True)))
    except uploads.UploadError as e:
        caller.audit(request, "report.run", 400, str(e), project["id"])
        raise HTTPException(status_code=400, detail=str(e))
    rows = report["rows"]
    if not rows:
        label = report_types.platform(platform).label
        detail = f"None of those {len(links)} links are {label} posts."
        caller.audit(request, "report.run", 400, detail, project["id"])
        raise HTTPException(status_code=400, detail=detail)

    # A TEST key answers in the same shape without spending a capture slot, so
    # an integration can be wired up and demoed before it costs anything.
    if caller.is_test:
        caller.audit(request, "report.run", 200, "test key — not captured",
                     project["id"])
        return JSONResponse({"run_id": "test", "status": "done", "test": True,
                             "link_count": len(rows), "project_id": project["id"],
                             "artifacts": ["pdf"]}, status_code=200)

    settings = project.get("settings") or {}
    try:
        job_ids = await runs.create_run_async(
            project, rows, "\n".join(links).encode("utf-8"),
            f"api:{caller.bot['name']}", name,
            types=styles or None, outputs=outputs,
            user=caller.username or "api",
            fetch_metrics=bool(settings.get("fetch_metrics")),
            fast_capture=bool(settings.get("fast_capture")),
            note=f"Started from /v1 by {caller.actor or caller.bot['name']}")
    except runs.RunError as e:
        caller.audit(request, "report.run", 400, str(e), project["id"])
        raise HTTPException(status_code=400, detail=str(e))

    caller.audit(request, "report.run", 202, project_id=project["id"],
                 job_id=job_ids[0])
    return JSONResponse({"run_id": job_ids[0], "run_ids": job_ids,
                         "status": "queued", "link_count": len(rows),
                         "project_id": project["id"]}, status_code=202)


# --------------------------------------------------------------------------- #
# The same two calls, from a spreadsheet
#
# `/v1/run` is JSON because "here are some links" is a JSON-shaped thought. A
# .xlsx is not, and the Telegram bot has always accepted one, so it gets its own
# route rather than a base64 field in the JSON body. Both go through the very
# same `_grid_from_request` the browser uses, so a sheet read here means exactly
# what it means on the New run page.
# --------------------------------------------------------------------------- #
async def _rows_from_upload(file, text: str, sheet_url: str, sheet: str,
                            platform: str, dedupe: bool):
    grid, source, raw = await _grid_from_request(file, text, sheet_url, sheet)
    report = await asyncio.to_thread(uploads.analyse, grid, dedupe, platform)
    return report, source, raw


@router.post("/preview/file")
async def preview_file(request: Request,
                       file: UploadFile = File(None),
                       text: str = Form(""),
                       sheet_url: str = Form(""),
                       sheet: str = Form(""),
                       project_id: str = Form(""),
                       styles: str = Form(""),
                       dedupe: str = Form("1"),
                       caller: Caller = Depends(require_token)):
    caller.require(request, "report.preview")
    project = _project_for(caller, request, project_id)
    want = [s for s in (styles or "").split(",") if s.strip()]
    platform = _style_platform(project, want)
    try:
        report, source, _raw = await _rows_from_upload(
            file, text, sheet_url, sheet, platform,
            dedupe.lower() not in ("", "0", "false", "off"))
    except HTTPException:
        raise
    except Exception as e:
        caller.audit(request, "report.preview", 400, str(e), project["id"])
        raise HTTPException(status_code=400, detail=str(e))
    caller.audit(request, "report.preview", 200, project_id=project["id"])
    return {"project_id": project["id"], "platform": platform, "source": source,
            "count": len(report["rows"]),
            "duplicate_count": report["duplicate_count"],
            "dropped_count": len(report["dropped"]),
            "dropped": report["dropped"][:50]}


@router.post("/run/file")
async def run_file(request: Request,
                   file: UploadFile = File(None),
                   text: str = Form(""),
                   sheet_url: str = Form(""),
                   sheet: str = Form(""),
                   name: str = Form(""),
                   project_id: str = Form(""),
                   styles: str = Form(""),
                   outputs: str = Form(""),
                   dedupe: str = Form("1"),
                   caller: Caller = Depends(require_token)):
    caller.require(request, "report.run")
    _check_rate(caller, request)
    project = _project_for(caller, request, project_id)
    want = [s for s in (styles or "").split(",") if s.strip()]
    platform = _style_platform(project, want)
    try:
        report, source, raw = await _rows_from_upload(
            file, text, sheet_url, sheet, platform,
            dedupe.lower() not in ("", "0", "false", "off"))
    except HTTPException:
        raise
    except Exception as e:
        caller.audit(request, "report.run", 400, str(e), project["id"])
        raise HTTPException(status_code=400, detail=str(e))

    rows = report["rows"]
    if not rows:
        label = report_types.platform(platform).label
        detail = f"No {label} post links in that input."
        caller.audit(request, "report.run", 400, detail, project["id"])
        raise HTTPException(status_code=400, detail=detail)
    cap = int((caller.bot.get("limits") or {}).get("links_per_call")
              or MAX_LINKS_PER_CALL)
    if len(rows) > cap:
        detail = f"{len(rows)} links — this bot's limit is {cap} per call."
        caller.audit(request, "report.run", 400, detail, project["id"])
        raise HTTPException(status_code=400, detail=detail)

    if caller.is_test:
        caller.audit(request, "report.run", 200, "test key — not captured",
                     project["id"])
        return JSONResponse({"run_id": "test", "status": "done", "test": True,
                             "link_count": len(rows), "project_id": project["id"],
                             "artifacts": ["pdf"]}, status_code=200)

    settings = project.get("settings") or {}
    try:
        job_ids = await runs.create_run_async(
            project, rows, raw, source,
            name.strip() or time.strftime("Report %d-%m-%y %H:%M"),
            types=want or None,
            outputs=[o for o in (outputs or "").split(",") if o.strip()],
            user=caller.username or "api",
            fetch_metrics=bool(settings.get("fetch_metrics")),
            fast_capture=bool(settings.get("fast_capture")),
            note=f"Started from /v1 by {caller.actor or caller.bot['name']}")
    except runs.RunError as e:
        caller.audit(request, "report.run", 400, str(e), project["id"])
        raise HTTPException(status_code=400, detail=str(e))

    caller.audit(request, "report.run", 202, project_id=project["id"],
                 job_id=job_ids[0])
    return JSONResponse({"run_id": job_ids[0], "run_ids": job_ids,
                         "status": "queued", "link_count": len(rows),
                         "project_id": project["id"]}, status_code=202)

def _run_for(caller: Caller, request: Request, run_id: str, scope: str) -> dict:
    job = store.get(run_id)
    if not job:
        caller.audit(request, scope, 404, "run not found")
        raise HTTPException(status_code=404, detail="Run not found.")
    allowed = caller.bot.get("projects") or []
    if allowed and (job.get("project_id") or "") not in allowed:
        detail = "That run belongs to a project this bot may not touch."
        caller.audit(request, scope, 403, detail, job_id=run_id)
        raise HTTPException(status_code=403, detail=detail)
    return job


@router.get("/run/{run_id}")
async def run_status(run_id: str, request: Request,
                     caller: Caller = Depends(require_token)):
    caller.require(request, "report.preview")
    job = _run_for(caller, request, run_id, "report.preview")
    caller.audit(request, "report.preview", 200,
                 project_id=job.get("project_id") or "", job_id=run_id)
    out = public_job(job)
    out["run_id"] = out["id"]
    return out


@router.post("/run/{run_id}/cancel")
async def run_cancel(run_id: str, request: Request,
                     caller: Caller = Depends(require_token)):
    caller.require(request, "report.cancel")
    job = _run_for(caller, request, run_id, "report.cancel")
    from .jobs import queue
    queue.cancel(run_id)
    caller.audit(request, "report.cancel", 200,
                 project_id=job.get("project_id") or "", job_id=run_id)
    return {"ok": True, "run_id": run_id}


@router.get("/run/{run_id}/download/{kind}")
async def run_download(run_id: str, kind: str, request: Request,
                       caller: Caller = Depends(require_token)):
    caller.require(request, "report.download")
    if kind not in _KINDS:
        raise HTTPException(status_code=404, detail="Unknown format.")
    job = _run_for(caller, request, run_id, "report.download")
    filename = (job.get("artifacts") or {}).get(kind)
    if not filename:
        detail = f"This run has no {kind.upper()}."
        caller.audit(request, "report.download", 404, detail, job_id=run_id)
        raise HTTPException(status_code=404, detail=detail)

    # Same resolution as the browser download: the stored name is
    # server-generated, and it is confirmed to be inside this job's own out/
    # folder before anything is served. No caller string ever becomes a path.
    out = runner.out_dir(run_id).resolve()
    path = (out / filename).resolve()
    if not str(path).startswith(str(out) + "/") or not path.is_file():
        caller.audit(request, "report.download", 404, "file gone", job_id=run_id)
        raise HTTPException(status_code=404, detail="That file is not available.")

    caller.audit(request, "report.download", 200,
                 project_id=job.get("project_id") or "", job_id=run_id)
    stem = uploads.download_name(job.get("title") or job["name"])
    suffix = "_screenshots.zip" if kind == "zip" else f".{kind}"
    return FileResponse(path, media_type=_KINDS[kind],
                        filename=f"{stem}{suffix}")

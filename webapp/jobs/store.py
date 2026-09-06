"""SQLite-backed job records + login-attempt tracking.

One file, no service to run, and job state survives a restart — which matters
because a crashed container must not leave the UI claiming a job is still
running. A connection is opened per call (cheap, and thread-safe by
construction, since jobs are executed on a worker thread pool).
"""
import datetime
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .. import config

# Terminal states — a job in one of these will never change again.
DONE_STATES = ("done", "failed", "cancelled", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    name          TEXT NOT NULL,
    title         TEXT NOT NULL,
    report_type   TEXT NOT NULL,
    status        TEXT NOT NULL,
    phase         TEXT DEFAULT '',
    total         INTEGER DEFAULT 0,
    done          INTEGER DEFAULT 0,
    link_count    INTEGER DEFAULT 0,
    upload_name   TEXT DEFAULT '',
    keep_engagement INTEGER DEFAULT 0,
    workers       INTEGER DEFAULT 0,
    outputs       TEXT DEFAULT '[]',
    error         TEXT DEFAULT '',
    artifacts     TEXT DEFAULT '{}',
    skipped       TEXT DEFAULT '[]',
    activity      TEXT DEFAULT '[]',
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs (owner, created_at DESC);

CREATE TABLE IF NOT EXISTS presets (
    id            TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    name          TEXT NOT NULL,
    platform      TEXT NOT NULL DEFAULT 'x',
    report_type   TEXT NOT NULL,
    keep_engagement INTEGER DEFAULT 0,
    workers       INTEGER DEFAULT 0,
    outputs       TEXT DEFAULT '[]',
    dedupe        INTEGER DEFAULT 1,
    sheet_url     TEXT DEFAULT '',
    report_name   TEXT DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS presets_owner ON presets (owner, created_at DESC);

-- v3: a PROJECT is a client / recurring report. It owns which styles print
-- it, and every job belongs to exactly one project. Projects are shared by the
-- whole team (owner is who made it, for the record); the dropdown in the left
-- bar switches which one the pages show.
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    client        TEXT DEFAULT '',
    emoji         TEXT DEFAULT '',
    owner         TEXT NOT NULL,
    settings      TEXT DEFAULT '{}',
    archived      INTEGER DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_styles (
    project_id    TEXT NOT NULL,
    slug          TEXT NOT NULL,
    outputs       TEXT DEFAULT '[]',
    position      INTEGER DEFAULT 0,
    PRIMARY KEY (project_id, slug)
);

-- v3: a SOURCE is where a project's links come from and keep coming from —
-- today a Google Sheet that the sync loop re-reads. `mode` = latest | tab | all
-- (see smartsheet.read); `auto_run` = start a run when the fingerprint changes.
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'sheet',
    label         TEXT DEFAULT '',
    url           TEXT NOT NULL,
    mode          TEXT DEFAULT 'latest',
    gid           TEXT DEFAULT '',
    auto_run      INTEGER DEFAULT 1,
    trigger       TEXT DEFAULT 'new_date',
    enabled       INTEGER DEFAULT 1,
    last_fingerprint TEXT DEFAULT '',
    last_date     TEXT DEFAULT '',
    last_tab      TEXT DEFAULT '',
    last_count    INTEGER DEFAULT 0,
    last_checked_at REAL,
    last_changed_at REAL,
    last_error    TEXT DEFAULT '',
    last_job_ids  TEXT DEFAULT '[]',
    log           TEXT DEFAULT '[]',
    created_by    TEXT DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sources_project ON sources (project_id, created_at);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    pw_hash       TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    REAL NOT NULL,
    created_by    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS login_attempts (
    ip   TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_ip_ts ON login_attempts (ip, ts);

-- v3.1: the BOT REGISTRY. A bot is a token that may touch some projects with
-- some scopes. A person acting through it is named by the `X-Actor` header, so
-- the rule the whole safety story rests on can be evaluated in one place:
--
--     effective permission = the token's scopes  n  the acting person's role
--
-- A bot can never exceed its token; a person can never exceed their account.
CREATE TABLE IF NOT EXISTS bots (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'telegram',
    token_hash    TEXT NOT NULL,
    token_hint    TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    projects      TEXT DEFAULT '[]',
    audience      TEXT DEFAULT '[]',
    limits        TEXT DEFAULT '{}',
    is_test       INTEGER DEFAULT 0,
    created_by    TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    last_used_at  REAL
);
CREATE INDEX IF NOT EXISTS bots_token ON bots (token_hash);

CREATE TABLE IF NOT EXISTS bot_scopes (
    bot_id        TEXT NOT NULL,
    scope         TEXT NOT NULL,
    PRIMARY KEY (bot_id, scope)
);

-- Which Report Maker account a Telegram id acts as. No row = the actor is a
-- stranger, and the intersection above leaves them with nothing.
CREATE TABLE IF NOT EXISTS tg_identities (
    actor         TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    label         TEXT DEFAULT '',
    linked_by     TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    last_seen_at  REAL
);

-- One-shot codes that bind a Telegram id to an account: an admin generates
-- one, the colleague sends `/link 482913` to the bot, the row is consumed.
CREATE TABLE IF NOT EXISTS link_codes (
    code          TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    created_by    TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    used_at       REAL,
    used_by       TEXT DEFAULT ''
);

-- The audit log. Every /v1 call lands here, refusals included -- a log that
-- only records successes cannot answer the question it exists for.
CREATE TABLE IF NOT EXISTS api_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id        TEXT DEFAULT '',
    bot_name      TEXT DEFAULT '',
    actor         TEXT DEFAULT '',
    username      TEXT DEFAULT '',
    scope         TEXT DEFAULT '',
    method        TEXT DEFAULT '',
    path          TEXT DEFAULT '',
    project_id    TEXT DEFAULT '',
    job_id        TEXT DEFAULT '',
    status        INTEGER DEFAULT 0,
    detail        TEXT DEFAULT '',
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS api_calls_bot_ts ON api_calls (bot_id, ts DESC);
CREATE INDEX IF NOT EXISTS api_calls_ts ON api_calls (ts DESC);

-- A FEATURE is a piece of a bot that can be switched off without being taken
-- out. Rows are ANNOUNCED by the bot itself on start-up -- it knows what it can
-- do; the dashboard does not, and hardcoding a list here would mean editing the
-- server every time a bot grows a button. `override` is the admin's answer:
-- NULL means "whatever the bot says its default is".
--
-- This is deliberately NOT the scopes table. A scope answers "may this person
-- do X" and belongs in the audit log; a feature answers "is X here at all".
-- Anything that could hurt somebody stays a scope.
CREATE TABLE IF NOT EXISTS bot_features (
    bot_id        TEXT NOT NULL,
    name          TEXT NOT NULL,
    label         TEXT DEFAULT '',
    why           TEXT DEFAULT '',
    default_on    INTEGER DEFAULT 1,
    override      INTEGER,
    announced_at  REAL,
    PRIMARY KEY (bot_id, name)
);
"""

_JSON_FIELDS = ("artifacts", "skipped", "activity", "outputs")

# Columns added after the first release, per table. `CREATE TABLE IF NOT EXISTS`
# is a no-op on a database that already has the table, so a new column has to be
# ALTERed in or every query against an existing deployment's DB fails.
# `outputs` = the formats the user ticked; [] means "everything the style
# builds", which is what every job created before 2.4.0 meant.
_ADDED_COLUMNS = {
    "jobs": (("keep_engagement", "INTEGER DEFAULT 0"),
             ("workers", "INTEGER DEFAULT 0"),          # 0 = the server default
             ("outputs", "TEXT DEFAULT '[]'"),
             ("project_id", "TEXT DEFAULT ''"),           # v3
             # Read likes/reposts/replies/views off each X post before the
             # document is built, and fill in the sheet columns that were left
             # blank. Off by default: it costs one page load per link and
             # spends the X account's daily budget (RULEBOOK rule 21).
             ("fetch_metrics", "INTEGER DEFAULT 0"),
             # Shorter fixed waits inside the capture (approved edit 6c).
             ("fast_capture", "INTEGER DEFAULT 0"),
             # The job whose screenshots this one started from (approved
             # edit 7). Empty for every job that began from scratch.
             ("resumed_from", "TEXT DEFAULT ''"),
             # The day the links belong to (the sheet's day tab), YYYY-MM-DD.
             # What the Client Portal dates a post by; empty = the run's day.
             ("sheet_date", "TEXT DEFAULT ''")),
    "presets": (("outputs", "TEXT DEFAULT '[]'"),),
    # Which of the project's styles THIS source runs. '[]' = all of them, which
    # is what every source created before this column meant.
    "sources": (("styles", "TEXT DEFAULT '[]'"),),
}

# Every job made before v3 lands here, so nothing is lost and nothing is
# orphaned. Created on first boot; cannot be deleted while it holds jobs.
UNSORTED_SLUG = "unsorted"


def _connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init() -> None:
    """Create the schema and clear out any job left 'running' by a crash."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name in have:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError:
                    # Another process added it between the PRAGMA and here.
                    # Harmless — the column exists either way, which is all we
                    # needed.
                    pass
        # A restart kills any capture that was in flight. Free hosts restart on
        # their own (rebuilds, idle sleep), so say plainly what to do next.
        conn.execute(
            "UPDATE jobs SET status='interrupted', phase='Interrupted', "
            "error='The server restarted while this job was running, so it did "
            "not finish. Please submit it again.', "
            "finished_at=? WHERE status IN ('running','queued')",
            (time.time(),))
        # v3 migration: an "Unsorted" project for every job that predates
        # projects. Idempotent — the second boot finds nothing to move.
        row = conn.execute("SELECT id FROM projects WHERE slug=?",
                           (UNSORTED_SLUG,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO projects (id, slug, name, client, emoji, owner, "
                "settings, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:12], UNSORTED_SLUG, "Unsorted (v2 reports)",
                 "", "🗂️", "system", "{}", time.time()))
            row = conn.execute("SELECT id FROM projects WHERE slug=?",
                               (UNSORTED_SLUG,)).fetchone()
        conn.execute("UPDATE jobs SET project_id=? WHERE project_id='' "
                     "OR project_id IS NULL", (row["id"],))


def _row_to_dict(row) -> dict:
    d = dict(row)
    for f in _JSON_FIELDS:
        try:
            d[f] = json.loads(d.get(f) or ("{}" if f == "artifacts" else "[]"))
        except (ValueError, TypeError):
            d[f] = {} if f == "artifacts" else []
    return d


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
def create(owner: str, name: str, title: str, report_type: str,
           link_count: int, upload_name: str,
           keep_engagement: bool = False, workers: int = 0,
           outputs=None, project_id: str = "",
           fetch_metrics: bool = False, fast_capture: bool = False,
           resumed_from: str = "") -> str:
    """`workers` = browsers to capture with; 0 means "use the server default".
    `outputs` = the formats ticked on the form; [] means every format the
    style builds. `fetch_metrics` = read each X post's likes / reposts /
    replies / views before building, and fill in the sheet columns left blank.
    `fast_capture` = shorten the waits every post pays regardless (edit 6c)."""
    job_id = uuid.uuid4().hex[:16]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, owner, name, title, report_type, status, "
            "phase, link_count, upload_name, total, keep_engagement, workers, "
            "outputs, project_id, fetch_metrics, fast_capture, resumed_from, "
            "created_at) "
            "VALUES (?,?,?,?,?,'queued','Waiting for a free capture slot',?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, owner, name, title, report_type, link_count, upload_name,
             link_count, int(bool(keep_engagement)), max(0, int(workers)),
             json.dumps(list(outputs or [])), project_id or "",
             int(bool(fetch_metrics)), int(bool(fast_capture)),
             resumed_from or "", time.time()))
    return job_id


def get(job_id: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_for(owner: str, limit: int = 30) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE owner=? ORDER BY created_at DESC LIMIT ?",
            (owner, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_for_project(project_id: str, limit: int = 200) -> list:
    """Every job in one project, newest first — projects are shared by the
    team, so this is not filtered by owner."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC "
            "LIMIT ?", (project_id, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_for_project(project_id: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE project_id=?",
                           (project_id,)).fetchone()
    return int(row["n"])


def list_all(limit: int = 500) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def captures_today() -> int:
    """Posts captured since local midnight, across every user.

    Deliberately server-wide rather than per-user: the thing being spent is one
    shared X account's daily headroom (RULEBOOK rule 21), so a per-user number
    would show each person plenty of room while the account was already spent.

    Counts `done` — captures actually taken — not `link_count`, so a cancelled
    or half-finished job is charged for the browser time it really used.
    """
    midnight = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(done), 0) AS n FROM jobs WHERE created_at >= ?",
            (midnight,)).fetchone()
    return int(row["n"])


def update(job_id: str, **fields) -> None:
    """Patch any subset of columns. JSON-typed fields are encoded here."""
    if not fields:
        return
    for f in _JSON_FIELDS:
        if f in fields and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?",
                     (*fields.values(), job_id))


def append_activity(job_id: str, message: str, level: str = "info") -> None:
    """Add one line to the job's activity log (what the UI's log panel shows).

    Capped so a pathological run can't grow the row without bound.
    """
    with _connect() as conn:
        row = conn.execute("SELECT activity FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        try:
            log = json.loads(row["activity"] or "[]")
        except (ValueError, TypeError):
            log = []
        log.append({"t": time.time(), "level": level, "message": message})
        conn.execute("UPDATE jobs SET activity=? WHERE id=?",
                     (json.dumps(log[-400:]), job_id))


def delete(job_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


# --------------------------------------------------------------------------- #
# Presets — a saved set of form choices, per user. Never stores a file: an
# upload cannot be re-run, only a Google Sheet or the paste box can, so a preset
# carries the sheet URL (optional) and the options, and the user supplies links.
# --------------------------------------------------------------------------- #
def preset_create(owner: str, name: str, platform: str, report_type: str,
                  keep_engagement: bool = False, workers: int = 0,
                  dedupe: bool = True, sheet_url: str = "",
                  report_name: str = "", outputs=None) -> str:
    pid = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO presets (id, owner, name, platform, report_type, "
            "keep_engagement, workers, outputs, dedupe, sheet_url, "
            "report_name, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, owner, name[:80], platform, report_type,
             int(bool(keep_engagement)), max(0, int(workers)),
             json.dumps(list(outputs or [])),
             int(bool(dedupe)), sheet_url[:500], report_name[:80], time.time()))
    return pid


def presets_for(owner: str, limit: int = 50) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM presets WHERE owner=? ORDER BY created_at DESC "
            "LIMIT ?", (owner, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["outputs"] = json.loads(d.get("outputs") or "[]")
        except (ValueError, TypeError):
            d["outputs"] = []
        out.append(d)
    return out


def preset_delete(owner: str, pid: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM presets WHERE id=? AND owner=?",
                           (pid, owner))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Projects (v3)
# --------------------------------------------------------------------------- #
def _project_row(r) -> dict:
    d = dict(r)
    try:
        d["settings"] = json.loads(d.get("settings") or "{}")
    except (ValueError, TypeError):
        d["settings"] = {}
    d["archived"] = bool(d.get("archived"))
    return d


def project_create(owner: str, slug: str, name: str, client: str = "",
                   emoji: str = "", settings: dict = None) -> str:
    pid = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, slug, name, client, emoji, owner, "
            "settings, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, slug, name[:80], (client or "")[:80], (emoji or "")[:8],
             owner, json.dumps(settings or {}), time.time()))
    return pid


def project_get(pid: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return _project_row(row) if row else None


def project_by_slug(slug: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    return _project_row(row) if row else None


def projects_list(include_archived: bool = False) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM projects " +
            ("" if include_archived else "WHERE archived=0 ") +
            "ORDER BY (slug=?) ASC, name COLLATE NOCASE ASC",
            (UNSORTED_SLUG,)).fetchall()
    return [_project_row(r) for r in rows]


def project_update(pid: str, **fields) -> None:
    if not fields:
        return
    if "settings" in fields and not isinstance(fields["settings"], str):
        fields["settings"] = json.dumps(fields["settings"])
    if "archived" in fields:
        fields["archived"] = int(bool(fields["archived"]))
    cols = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE projects SET {cols} WHERE id=?",
                     (*fields.values(), pid))


def project_delete(pid: str) -> bool:
    """Only an EMPTY project can be deleted; one with jobs is archived
    instead, so history is never silently lost."""
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE project_id=?",
                         (pid,)).fetchone()["n"]
        if n:
            return False
        conn.execute("DELETE FROM project_styles WHERE project_id=?", (pid,))
        cur = conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    return cur.rowcount > 0


def project_job_ids(pid: str) -> list:
    """Every job id the project owns, any status."""
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM jobs WHERE project_id=?", (pid,)).fetchall()
    return [r["id"] for r in rows]


def project_active_jobs(pid: str) -> int:
    """Jobs still queued or running — a project with any of these is not
    deletable until they finish or are stopped."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE project_id=? "
            "AND status IN ('running','queued')", (pid,)).fetchone()
    return int(row["n"])


def projects_using_style(slug: str) -> list:
    """Ids of every project (archived ones too) that has this style attached."""
    with _connect() as conn:
        rows = conn.execute("SELECT project_id FROM project_styles WHERE slug=?",
                            (slug,)).fetchall()
    return [r["project_id"] for r in rows]


def project_purge(pid: str) -> dict:
    """Remove the project and every row that hangs off it — jobs, sources and
    its style picks — in one transaction. Files on disk are the caller's job
    (`projects.delete_project` does both). Returns the row counts removed."""
    with _connect() as conn:
        jobs = conn.execute("DELETE FROM jobs WHERE project_id=?", (pid,)).rowcount
        sources = conn.execute("DELETE FROM sources WHERE project_id=?", (pid,)).rowcount
        conn.execute("DELETE FROM project_styles WHERE project_id=?", (pid,))
        cur = conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    return {"jobs": jobs, "sources": sources, "project": cur.rowcount}


def project_styles(pid: str) -> list:
    """[{slug, outputs}] in the project's own order."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT slug, outputs FROM project_styles WHERE project_id=? "
            "ORDER BY position ASC", (pid,)).fetchall()
    out = []
    for r in rows:
        try:
            outs = json.loads(r["outputs"] or "[]")
        except (ValueError, TypeError):
            outs = []
        out.append({"slug": r["slug"], "outputs": outs})
    return out


def project_set_styles(pid: str, items: list) -> None:
    """Replace the project's style list. `items` = [{slug, outputs}]."""
    with _connect() as conn:
        conn.execute("DELETE FROM project_styles WHERE project_id=?", (pid,))
        for i, it in enumerate(items):
            conn.execute(
                "INSERT INTO project_styles (project_id, slug, outputs, position) "
                "VALUES (?,?,?,?)",
                (pid, it["slug"], json.dumps(list(it.get("outputs") or [])), i))


def project_replace_style(pid: str, old_slug: str, new_slug: str) -> None:
    """Swap one style for another in place (used when a shipped style is
    copied so a project can give it its own background)."""
    with _connect() as conn:
        conn.execute("UPDATE project_styles SET slug=? WHERE project_id=? AND slug=?",
                     (new_slug, pid, old_slug))


# --------------------------------------------------------------------------- #
# Sources (v3)
# --------------------------------------------------------------------------- #
def _source_row(r) -> dict:
    d = dict(r)
    for f in ("last_job_ids", "log", "styles"):
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except (ValueError, TypeError):
            d[f] = []
    d["auto_run"] = bool(d.get("auto_run"))
    d["enabled"] = bool(d.get("enabled"))
    return d


def source_create(project_id: str, url: str, mode: str = "latest", gid: str = "",
                  auto_run: bool = True, label: str = "", created_by: str = "",
                  kind: str = "sheet", trigger: str = "new_date",
                  styles=None) -> str:
    """`trigger`: 'new_date' = run only when the newest date moves on (a new
    day tab / a new date block); 'any_change' = run whenever links change.

    `styles`: slugs of the project's styles this source runs with. Empty means
    every runnable style the project has — the behaviour before the column
    existed, so an untouched source keeps working exactly as it did.
    """
    sid = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sources (id, project_id, kind, label, url, mode, gid, "
            "auto_run, trigger, styles, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, project_id, kind, (label or "")[:80], url[:600], mode, gid or "",
             int(bool(auto_run)), trigger if trigger in ("new_date", "any_change") else "new_date",
             json.dumps([str(s) for s in (styles or [])]), created_by, time.time()))
    return sid


def source_get(sid: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    return _source_row(row) if row else None


def sources_for(project_id: str) -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sources WHERE project_id=? "
                            "ORDER BY created_at ASC", (project_id,)).fetchall()
    return [_source_row(r) for r in rows]


def sources_all(enabled_only: bool = True) -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sources " +
                            ("WHERE enabled=1 " if enabled_only else "") +
                            "ORDER BY created_at ASC").fetchall()
    return [_source_row(r) for r in rows]


def source_update(sid: str, **fields) -> None:
    if not fields:
        return
    for f in ("last_job_ids", "log", "styles"):
        if f in fields and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f])
    for f in ("auto_run", "enabled"):
        if f in fields:
            fields[f] = int(bool(fields[f]))
    cols = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), sid))


def source_log(sid: str, message: str, level: str = "info") -> None:
    with _connect() as conn:
        row = conn.execute("SELECT log FROM sources WHERE id=?", (sid,)).fetchone()
        if row is None:
            return
        try:
            log = json.loads(row["log"] or "[]")
        except (ValueError, TypeError):
            log = []
        log.append({"t": time.time(), "level": level, "message": message})
        conn.execute("UPDATE sources SET log=? WHERE id=?", (json.dumps(log[-60:]), sid))


def source_delete(sid: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Users managed in the app (Admin → Users). `.env` APP_USERS keeps working as
# the bootstrap / break-glass login; anything created here lives in the DB.
# --------------------------------------------------------------------------- #
ROLES = ("admin", "designer", "member")


def users_list() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT username, role, created_at, created_by "
                            "FROM users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def user_get(username: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def user_upsert(username: str, pw_hash: str, role: str, created_by: str = "") -> None:
    role = role if role in ROLES else "member"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, pw_hash, role, created_at, created_by) "
            "VALUES (?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
            "pw_hash=excluded.pw_hash, role=excluded.role",
            (username, pw_hash, role, time.time(), created_by))


def user_set_role(username: str, role: str) -> bool:
    if role not in ROLES:
        return False
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))
    return cur.rowcount > 0


def user_set_password(username: str, pw_hash: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET pw_hash=? WHERE username=?", (pw_hash, username))
    return cur.rowcount > 0


def user_delete(username: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Login rate limiting
# --------------------------------------------------------------------------- #
def record_login_failure(ip: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO login_attempts (ip, ts) VALUES (?,?)",
                     (ip, time.time()))


def clear_login_failures(ip: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))


def recent_login_failures(ip: str) -> int:
    cutoff = time.time() - config.LOGIN_WINDOW_MINUTES * 60
    with _connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ts < ?", (cutoff,))
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE ip=? AND ts >= ?",
            (ip, cutoff)).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------- #
# v3.1 — the bot registry
#
# Tokens are stored HASHED, exactly like passwords: a leaked database must not
# hand anybody a working key. `token_hint` is the last four characters, kept in
# clear so a human can tell two keys apart on the Bots page without either of
# them being reconstructable from it.
# --------------------------------------------------------------------------- #
TOKEN_PREFIX = "vr_"

# Every scope the server knows. The Bots page offers exactly these, so a typo
# in a preset can never grant something the API does not check for.
SCOPES = (
    "project.read",       # list projects and their styles
    "report.preview",     # what WOULD be captured, costing nothing
    "report.run",         # spend capture minutes
    "report.download",    # fetch a finished artifact
    "report.cancel",      # stop a run
    "source.run",         # make a source re-read now
    "send.compose",       # draft a batch of messages
    "send.dispatch",      # actually send them
    "send.other_chat",    # ...into a chat the sender is not standing in
    "style.write",
    "schedule.write",
    "users.link",         # bind a Telegram id to an account
)

# One click instead of twelve tick boxes. The names are the plan's (§2.2).
SCOPE_PRESETS = {
    "viewer":    ("project.read", "report.preview", "report.download"),
    "operator":  ("project.read", "report.preview", "report.run",
                  "report.download", "report.cancel", "source.run"),
    "publisher": ("project.read", "report.preview", "report.run",
                  "report.download", "report.cancel", "source.run",
                  "send.compose", "send.dispatch"),
    "admin":     SCOPES,
}

# What a role may do at most, whatever the token says. This is the right-hand
# side of  scopes n role  — see api_v1.effective_scopes.
ROLE_CEILING = {
    "admin": set(SCOPES),
    "designer": {"project.read", "report.preview", "report.run",
                 "report.download", "report.cancel", "source.run",
                 "send.compose", "send.dispatch", "style.write"},
    "member": {"project.read", "report.preview", "report.run",
               "report.download", "report.cancel", "send.compose",
               "send.dispatch"},
}

DEFAULT_LIMITS = {"links_per_call": 200, "runs_per_hour": 60, "mb_per_day": 500,
                  "max_concurrent": 2}


def hash_token(token: str) -> str:
    """sha256 of the raw token. A key is high-entropy and machine-generated, so
    unlike a password it needs no slow KDF — a rainbow table over 32 random
    bytes does not exist."""
    import hashlib
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_token(slug: str) -> str:
    """`vr_<slug>_<random>` — the shape docs/v3-plan.md §11 promised. The slug
    is cosmetic (it makes a key recognisable in someone's .env); every check
    goes through the hash."""
    import re as _re
    import secrets
    slug = _re.sub(r"[^a-z0-9]+", "-", (slug or "bot").lower()).strip("-")[:24] or "bot"
    return f"{TOKEN_PREFIX}{slug}_{secrets.token_urlsafe(24)}"


def _bot_row(r) -> dict:
    d = dict(r)
    for key in ("projects", "audience"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except (ValueError, TypeError):
            d[key] = []
    try:
        d["limits"] = {**DEFAULT_LIMITS, **(json.loads(d.get("limits") or "{}") or {})}
    except (ValueError, TypeError):
        d["limits"] = dict(DEFAULT_LIMITS)
    d["is_test"] = bool(d.get("is_test"))
    return d


def bot_create(name: str, kind: str = "telegram", scopes=(), projects=(),
               audience=(), limits: dict = None, is_test: bool = False,
               created_by: str = "") -> tuple:
    """Create a bot and return (record, raw_token). The raw token is returned
    exactly once and never stored — losing it means regenerating, which is the
    only honest way to hold a secret."""
    token = new_token(name)
    bot_id = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO bots (id, name, kind, token_hash, token_hint, status, "
            "projects, audience, limits, is_test, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (bot_id, name.strip()[:80] or "Bot", kind, hash_token(token),
             token[-4:], "active", json.dumps(list(projects or [])),
             json.dumps([str(a) for a in (audience or [])]),
             json.dumps(limits or {}), 1 if is_test else 0, created_by,
             time.time()))
        for scope in dict.fromkeys(s for s in (scopes or ()) if s in SCOPES):
            conn.execute("INSERT OR IGNORE INTO bot_scopes (bot_id, scope) "
                         "VALUES (?,?)", (bot_id, scope))
    return bot_get(bot_id), token


def bot_regenerate(bot_id: str):
    """New token for an existing bot, keeping its scopes, projects and history.
    Revocation and rotation are the same button pressed for different reasons."""
    bot = bot_get(bot_id)
    if not bot:
        return None, ""
    token = new_token(bot["name"])
    with _connect() as conn:
        conn.execute("UPDATE bots SET token_hash=?, token_hint=? WHERE id=?",
                     (hash_token(token), token[-4:], bot_id))
    return bot_get(bot_id), token


def bot_get(bot_id: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    return _bot_row(row) if row else None


def bot_by_token(token: str):
    """The bot a raw token belongs to, or None. Only ever matched on the hash."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM bots WHERE token_hash=?",
                           (hash_token(token),)).fetchone()
    return _bot_row(row) if row else None


def bots_list() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM bots ORDER BY created_at DESC").fetchall()
    return [_bot_row(r) for r in rows]


def bot_update(bot_id: str, **fields) -> None:
    allowed = {"name", "status", "projects", "audience", "limits", "kind"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("projects", "audience"):
            value = json.dumps([str(v) for v in (value or [])])
        elif key == "limits":
            value = json.dumps(value or {})
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    values.append(bot_id)
    with _connect() as conn:
        conn.execute(f"UPDATE bots SET {', '.join(sets)} WHERE id=?", values)


def bot_delete(bot_id: str) -> bool:
    with _connect() as conn:
        conn.execute("DELETE FROM bot_scopes WHERE bot_id=?", (bot_id,))
        cur = conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    return cur.rowcount > 0


def bot_scopes_of(bot_id: str) -> set:
    with _connect() as conn:
        rows = conn.execute("SELECT scope FROM bot_scopes WHERE bot_id=?",
                            (bot_id,)).fetchall()
    return {r["scope"] for r in rows}


def bot_set_scopes(bot_id: str, scopes) -> None:
    keep = [s for s in dict.fromkeys(scopes or ()) if s in SCOPES]
    with _connect() as conn:
        conn.execute("DELETE FROM bot_scopes WHERE bot_id=?", (bot_id,))
        for scope in keep:
            conn.execute("INSERT OR IGNORE INTO bot_scopes (bot_id, scope) "
                         "VALUES (?,?)", (bot_id, scope))


def bot_touch(bot_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE bots SET last_used_at=? WHERE id=?",
                     (time.time(), bot_id))


# --------------------------------------------------------------------------- #
# Telegram identities — who is acting behind a bot
# --------------------------------------------------------------------------- #
def identity_get(actor: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tg_identities WHERE actor=?",
                           (actor,)).fetchone()
    return dict(row) if row else None


def identities_list() -> list:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tg_identities ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def identity_set(actor: str, username: str, label: str = "",
                 linked_by: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tg_identities (actor, username, label, linked_by, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(actor) DO UPDATE SET "
            "username=excluded.username, label=excluded.label",
            (actor, username, label[:80], linked_by, time.time()))


def identity_seen(actor: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE tg_identities SET last_seen_at=? WHERE actor=?",
                     (time.time(), actor))


def identity_delete(actor: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tg_identities WHERE actor=?", (actor,))
    return cur.rowcount > 0


def link_code_create(username: str, created_by: str = "") -> str:
    import secrets
    code = f"{secrets.randbelow(900000) + 100000}"
    with _connect() as conn:
        conn.execute("DELETE FROM link_codes WHERE username=? AND used_at IS NULL",
                     (username,))
        conn.execute("INSERT INTO link_codes (code, username, created_by, created_at) "
                     "VALUES (?,?,?,?)", (code, username, created_by, time.time()))
    return code


def link_code_consume(code: str, actor: str, ttl_minutes: int = 30):
    """Spend a code and bind `actor` to its account. Returns the username, or
    None when the code is unknown, already spent or older than `ttl_minutes`."""
    now = time.time()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM link_codes WHERE code=?", (code,)).fetchone()
        if not row or row["used_at"] or now - row["created_at"] > ttl_minutes * 60:
            return None
        conn.execute("UPDATE link_codes SET used_at=?, used_by=? WHERE code=?",
                     (now, actor, code))
        username = row["username"]
    identity_set(actor, username, linked_by=row["created_by"] or "")
    return username


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def api_call_record(*, bot_id: str = "", bot_name: str = "", actor: str = "",
                    username: str = "", scope: str = "", method: str = "",
                    path: str = "", project_id: str = "", job_id: str = "",
                    status: int = 0, detail: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO api_calls (bot_id, bot_name, actor, username, scope, "
            "method, path, project_id, job_id, status, detail, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (bot_id, bot_name, actor, username, scope, method, path[:200],
             project_id, job_id, int(status), (detail or "")[:300], time.time()))


def api_calls_recent(limit: int = 100, bot_id: str = "") -> list:
    with _connect() as conn:
        if bot_id:
            rows = conn.execute(
                "SELECT * FROM api_calls WHERE bot_id=? ORDER BY ts DESC LIMIT ?",
                (bot_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM api_calls ORDER BY ts DESC LIMIT ?",
                                (limit,)).fetchall()
    return [dict(r) for r in rows]


def api_calls_since(bot_id: str, seconds: float, scope: str = "") -> int:
    """How many calls this bot has made in the last `seconds` — the rate limit
    is counted from the audit log rather than from memory, so a restart cannot
    hand somebody a fresh allowance."""
    cutoff = time.time() - seconds
    with _connect() as conn:
        if scope:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM api_calls WHERE bot_id=? AND ts>=? "
                "AND scope=? AND status < 400", (bot_id, cutoff, scope)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM api_calls WHERE bot_id=? AND ts>=? "
                "AND status < 400", (bot_id, cutoff)).fetchone()
    return int(row["n"])


def api_calls_prune(days: int = 30) -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM api_calls WHERE ts < ?",
                           (time.time() - days * 86400,))
    return cur.rowcount


# --------------------------------------------------------------------------- #
# Bot features — announced by the bot, overridden by an admin
# --------------------------------------------------------------------------- #
def bot_features_announce(bot_id: str, catalogue) -> None:
    """Record what this bot says it can do.

    Upsert on (bot_id, name) so an admin's `override` SURVIVES a redeploy —
    the bot re-announcing its catalogue must not quietly undo a decision
    somebody made on the dashboard. Labels and defaults are refreshed, because
    those belong to the bot; the override does not.
    """
    now = time.time()
    with _connect() as conn:
        seen = []
        for item in catalogue or ():
            name = str((item or {}).get("name") or "").strip()[:48]
            if not name:
                continue
            seen.append(name)
            conn.execute(
                "INSERT INTO bot_features (bot_id, name, label, why, default_on, "
                "announced_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(bot_id, name) DO UPDATE SET "
                "label=excluded.label, why=excluded.why, "
                "default_on=excluded.default_on, announced_at=excluded.announced_at",
                (bot_id, name, str(item.get("label") or name)[:120],
                 str(item.get("why") or "")[:400],
                 1 if item.get("default") else 0, now))
        # A feature the bot no longer has is dropped, so the page never offers a
        # switch that controls nothing.
        if seen:
            marks = ",".join("?" * len(seen))
            conn.execute(f"DELETE FROM bot_features WHERE bot_id=? AND name NOT IN ({marks})",
                         [bot_id, *seen])


def bot_features_list(bot_id: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_features WHERE bot_id=? ORDER BY name", (bot_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["default_on"] = bool(d["default_on"])
        d["override"] = None if d["override"] is None else bool(d["override"])
        d["on"] = d["default_on"] if d["override"] is None else d["override"]
        out.append(d)
    return out


def bot_features_effective(bot_id: str) -> dict:
    return {f["name"]: f["on"] for f in bot_features_list(bot_id)}


def bot_feature_set(bot_id: str, name: str, override) -> bool:
    """`override` True / False, or None to go back to the bot's own default."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE bot_features SET override=? WHERE bot_id=? AND name=?",
            (None if override is None else (1 if override else 0), bot_id, name))
    return cur.rowcount > 0

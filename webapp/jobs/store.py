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
             ("fast_capture", "INTEGER DEFAULT 0")),
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
           fetch_metrics: bool = False, fast_capture: bool = False) -> str:
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
            "outputs, project_id, fetch_metrics, fast_capture, created_at) "
            "VALUES (?,?,?,?,?,'queued','Waiting for a free capture slot',?,?,?,?,?,?,?,?,?,?)",
            (job_id, owner, name, title, report_type, link_count, upload_name,
             link_count, int(bool(keep_engagement)), max(0, int(workers)),
             json.dumps(list(outputs or [])), project_id or "",
             int(bool(fetch_metrics)), int(bool(fast_capture)), time.time()))
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

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
    dedupe        INTEGER DEFAULT 1,
    sheet_url     TEXT DEFAULT '',
    report_name   TEXT DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS presets_owner ON presets (owner, created_at DESC);

CREATE TABLE IF NOT EXISTS login_attempts (
    ip   TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_ip_ts ON login_attempts (ip, ts);
"""

_JSON_FIELDS = ("artifacts", "skipped", "activity")

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op
# on a database that already has the table, so a new column has to be ALTERed in
# or every query against an existing deployment's DB fails.
_ADDED_COLUMNS = (("keep_engagement", "INTEGER DEFAULT 0"),
                  ("workers", "INTEGER DEFAULT 0"))     # 0 = the server default


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
        have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for name, decl in _ADDED_COLUMNS:
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                # Another process added it between the PRAGMA and here. Harmless
                # — the column exists either way, which is all we needed.
                pass
        # A restart kills any capture that was in flight. Free hosts restart on
        # their own (rebuilds, idle sleep), so say plainly what to do next.
        conn.execute(
            "UPDATE jobs SET status='interrupted', phase='Interrupted', "
            "error='The server restarted while this job was running, so it did "
            "not finish. Please submit it again.', "
            "finished_at=? WHERE status IN ('running','queued')",
            (time.time(),))


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
           keep_engagement: bool = False, workers: int = 0) -> str:
    """`workers` = browsers to capture with; 0 means "use the server default"."""
    job_id = uuid.uuid4().hex[:16]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, owner, name, title, report_type, status, "
            "phase, link_count, upload_name, total, keep_engagement, workers, "
            "created_at) "
            "VALUES (?,?,?,?,?,'queued','Waiting for a free capture slot',?,?,?,?,?,?)",
            (job_id, owner, name, title, report_type, link_count, upload_name,
             link_count, int(bool(keep_engagement)), max(0, int(workers)),
             time.time()))
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
                  report_name: str = "") -> str:
    pid = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO presets (id, owner, name, platform, report_type, "
            "keep_engagement, workers, dedupe, sheet_url, report_name, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, owner, name[:80], platform, report_type,
             int(bool(keep_engagement)), max(0, int(workers)),
             int(bool(dedupe)), sheet_url[:500], report_name[:80], time.time()))
    return pid


def presets_for(owner: str, limit: int = 50) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM presets WHERE owner=? ORDER BY created_at DESC "
            "LIMIT ?", (owner, limit)).fetchall()
    return [dict(r) for r in rows]


def preset_delete(owner: str, pid: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM presets WHERE id=? AND owner=?",
                           (pid, owner))
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

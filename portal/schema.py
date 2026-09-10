"""The portal database — tables, indexes, and the one function that creates
them. Standard library only: `webapp/portal_publish.py` imports this, and it
must never pull FastAPI into the internal app by accident.

Two groups of tables live in the same file, on purpose:

* **Published data** — `clients`, `client_projects`, `client_sources`,
  `post_metrics`, `scraper_cache`, `publish_log`. Written by the INTERNAL tool
  (publish step, scraper sync, Admin → Clients). The portal only reads them.
* **Portal auth** — `client_users`, `client_sessions`, `client_login_attempts`,
  `portal_audit`. The portal's own; the internal tool only creates users
  (invites) and reads the audit log.

`visible_from` on `post_metrics` is the two-day rule as data: a row is shown
to the client when `visible_from <= today` in the client's timezone. Nothing
else decides visibility (`portal/db.py: visible_posts`).
"""
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS clients (
    id              TEXT PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    display_name    TEXT DEFAULT '',
    logo_path       TEXT DEFAULT '',
    accent_hex      TEXT DEFAULT '',
    lag_days        INTEGER NOT NULL DEFAULT 2,
    tz              TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    category_map    TEXT NOT NULL DEFAULT '{}',   -- {raw heading: {label, order, hidden}}
    show_screenshots INTEGER NOT NULL DEFAULT 0,
    show_reports    INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT DEFAULT '',
    created_at      REAL NOT NULL,
    archived        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS client_projects (
    client_id       TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    linked_by       TEXT DEFAULT '',
    linked_at       REAL NOT NULL,
    PRIMARY KEY (client_id, project_id)
);

-- The scraper that supplies post content + counts for a client. One active
-- source per client is the normal case; several are allowed (name them).
CREATE TABLE IF NOT EXISTS client_sources (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    signature_name  TEXT NOT NULL,                 -- the label for this key
    base_url        TEXT NOT NULL,                 -- may contain {from} {to} {date}
    api_key_enc     TEXT NOT NULL DEFAULT '',      -- portal/secretbox.py, never plain
    auth_style      TEXT NOT NULL DEFAULT 'bearer', -- bearer | x-api-key | query:<param>
    probe_url       TEXT NOT NULL DEFAULT '',      -- the handshake, e.g. .../api/project?project=16
    enabled         INTEGER NOT NULL DEFAULT 1,
    added_by        TEXT DEFAULT '',
    added_at        REAL NOT NULL,
    last_ok_at      REAL,
    last_error      TEXT DEFAULT '',
    last_count      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS client_sources_client ON client_sources (client_id);

-- THE fact table. One row per (client, post, sheet day).
CREATE TABLE IF NOT EXISTS post_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,
    project_id      TEXT DEFAULT '',
    run_id          TEXT DEFAULT '',
    sheet_date      TEXT NOT NULL,                 -- YYYY-MM-DD, the day tab
    visible_from    TEXT NOT NULL,                 -- sheet_date + clients.lag_days
    captured_at     REAL,
    platform        TEXT NOT NULL,                 -- x | facebook | instagram
    category_raw    TEXT NOT NULL DEFAULT '',      -- heading exactly as in the sheet
    handle          TEXT DEFAULT '',
    display_name    TEXT DEFAULT '',
    avatar_url      TEXT DEFAULT '',
    post_url        TEXT NOT NULL,
    post_url_norm   TEXT NOT NULL,
    tweet_id        TEXT NOT NULL DEFAULT '',      -- the platform's own post id, AS A STRING
    post_type       TEXT DEFAULT '',               -- post | reel | photo | video
    caption         TEXT DEFAULT '',
    lang            TEXT DEFAULT '',
    media_type      TEXT DEFAULT '',               -- image | video | ''
    thumb_url       TEXT DEFAULT '',
    posted_at       TEXT DEFAULT '',               -- ISO, as the scraper gave it
    collected_at    TEXT DEFAULT '',
    likes           INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    views           INTEGER,
    reach           INTEGER,
    impressions     INTEGER,
    quotes          INTEGER,
    bookmarks       INTEGER,
    author_followers INTEGER,
    last_refresh_ms INTEGER,                       -- when the Collector last read the numbers
    refresh_count   INTEGER,
    metric_source   TEXT NOT NULL DEFAULT 'none',  -- sheet | page | ocr | mixed | scraper | none
    raw_metrics     TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'ok',    -- ok | skipped
    skip_reason     TEXT DEFAULT '',
    screenshot_path TEXT DEFAULT '',
    first_published_at REAL NOT NULL,
    published_at    REAL NOT NULL,
    UNIQUE (client_id, post_url_norm, sheet_date)
);
CREATE INDEX IF NOT EXISTS pm_client_visible ON post_metrics (client_id, visible_from);
CREATE INDEX IF NOT EXISTS pm_client_day     ON post_metrics (client_id, sheet_date, category_raw, platform);
CREATE INDEX IF NOT EXISTS pm_client_url     ON post_metrics (client_id, post_url_norm);
-- NB: the index on `tweet_id` is created in ensure_schema(), AFTER the column
-- is ALTERed in. It cannot live here: on a database that already has
-- post_metrics, CREATE TABLE IF NOT EXISTS is a no-op, so this script would
-- reference a column that does not exist yet and the whole thing would fail.

-- THE history. `post_metrics` is current state and is overwritten in place by
-- every sync; this is the append-only time series behind every growth chart.
-- One row per post per PULL DAY (the client's timezone), never overwritten
-- except by a second pull on the same day.
--
-- `post_key` is the stable identity: the platform's own post id when the
-- Collector sent one, else the normalised URL. It is what makes a row findable
-- after somebody retypes a link with different capitalisation.
CREATE TABLE IF NOT EXISTS post_metric_days (
    client_id       TEXT NOT NULL,
    post_key        TEXT NOT NULL,                 -- tweet_id when known, else post_url_norm
    day             TEXT NOT NULL,                 -- the PULL day, YYYY-MM-DD in clients.tz
    tweet_id        TEXT NOT NULL DEFAULT '',
    post_url_norm   TEXT NOT NULL DEFAULT '',
    sheet_date      TEXT NOT NULL DEFAULT '',      -- the day the post is FILED under
    platform        TEXT NOT NULL DEFAULT '',
    category_raw    TEXT NOT NULL DEFAULT '',
    likes           INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    views           INTEGER,
    quotes          INTEGER,
    bookmarks       INTEGER,
    author_followers INTEGER,
    status          TEXT NOT NULL DEFAULT 'ok',
    last_refresh_ms INTEGER,
    refresh_count   INTEGER,
    metric_source   TEXT NOT NULL DEFAULT 'scraper',
    pulled_at       REAL NOT NULL,
    PRIMARY KEY (client_id, post_key, day)
);
CREATE INDEX IF NOT EXISTS pmd_client_day  ON post_metric_days (client_id, day);
CREATE INDEX IF NOT EXISTS pmd_client_post ON post_metric_days (client_id, post_key, day);

-- What the scraper answered for a (client, day), verbatim, so a page reload
-- never re-hits the scraper and a bad mapping can be re-run from the cache.
CREATE TABLE IF NOT EXISTS scraper_cache (
    client_id       TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    day             TEXT NOT NULL,
    fetched_at      REAL NOT NULL,
    post_count      INTEGER NOT NULL DEFAULT 0,
    body            TEXT NOT NULL,
    PRIMARY KEY (client_id, source_id, day)
);

CREATE TABLE IF NOT EXISTS publish_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,
    run_id          TEXT DEFAULT '',
    kind            TEXT NOT NULL,                 -- run | backfill | scraper
    sheet_date      TEXT DEFAULT '',
    posts           INTEGER NOT NULL DEFAULT 0,
    visible_from    TEXT DEFAULT '',
    note            TEXT DEFAULT '',
    by_user         TEXT DEFAULT '',
    at              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS publish_log_client ON publish_log (client_id, at DESC);

-- ---- portal auth ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_users (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    email           TEXT NOT NULL,
    username        TEXT NOT NULL DEFAULT '',   -- optional login alias set by staff
    pw_hash         TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'viewer', -- viewer | manager
    invited_by      TEXT DEFAULT '',
    invite_token    TEXT DEFAULT '',                -- sha256 of the token in the link
    invite_expires  REAL,
    created_at      REAL NOT NULL,
    last_login_at   REAL,
    disabled        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (client_id, email)
);
-- NB: the unique index on `username` is created in ensure_schema(), AFTER the
-- column is ALTERed in — same reason as pm_client_tweet above. Left here it
-- kills executescript() on any database created before the column existed,
-- which takes BOTH apps down at boot on exactly the deployments that have
-- data worth keeping.

CREATE TABLE IF NOT EXISTS client_sessions (
    sid             TEXT PRIMARY KEY,               -- sha256 of the cookie value
    user_id         TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    csrf            TEXT NOT NULL,
    created_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    ip              TEXT DEFAULT '',
    ua              TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS client_sessions_user ON client_sessions (user_id);

CREATE TABLE IF NOT EXISTS client_login_attempts (
    ip              TEXT NOT NULL,
    at              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS client_login_attempts_ip ON client_login_attempts (ip, at);

CREATE TABLE IF NOT EXISTS portal_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    client_id       TEXT DEFAULT '',
    user_id         TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    action          TEXT NOT NULL,                  -- login | login_failed | logout | export | invite_accepted | …
    detail          TEXT DEFAULT '',
    ip              TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS portal_audit_client ON portal_audit (client_id, at DESC);

-- Report files (PDF/PPTX/DOCX) the report tool built for a client's day.
-- Copied into PORTAL_MEDIA_DIR by webapp/portal_publish so the read-only
-- portal can serve them; shown once visible_from <= today, like posts.
CREATE TABLE IF NOT EXISTS client_reports (
    id            TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    project_id    TEXT DEFAULT '',
    run_id        TEXT DEFAULT '',
    sheet_date    TEXT NOT NULL,
    visible_from  TEXT NOT NULL,
    fmt           TEXT NOT NULL,              -- pdf | pptx | docx
    label         TEXT DEFAULT '',
    rel_path      TEXT NOT NULL,              -- under PORTAL_MEDIA_DIR
    bytes         INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    UNIQUE (client_id, run_id, fmt)
);
CREATE INDEX IF NOT EXISTS client_reports_client ON client_reports (client_id, sheet_date, visible_from);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""


def connect(path, read_only: bool = False) -> sqlite3.Connection:
    """A connection with row access by name. `read_only=True` opens the file
    with SQLite's own `mode=ro`, so a stray write raises instead of landing."""
    p = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10,
                               check_same_thread=False)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a
# no-op on a database that already has the table, so a new column has to be
# ALTERed in or every query against a deployed database fails. Same shape as
# `webapp/jobs/store.py: _ADDED_COLUMNS`, for the same reason.
_ADDED_COLUMNS = {
    "client_users": (("username", "TEXT NOT NULL DEFAULT ''"),),
    "client_sources": (("probe_url", "TEXT NOT NULL DEFAULT ''"),),
    # v2 — what the Collector sends that the portal had nowhere to put.
    "post_metrics": (("tweet_id", "TEXT NOT NULL DEFAULT ''"),
                     ("lang", "TEXT DEFAULT ''"),
                     ("quotes", "INTEGER"),
                     ("bookmarks", "INTEGER"),
                     ("author_followers", "INTEGER"),
                     ("last_refresh_ms", "INTEGER"),
                     ("refresh_count", "INTEGER")),
}


def ensure_schema(path) -> None:
    """Create every table that is missing and add every column that is new.
    Safe to call on every start of either app; a no-op on a database that is
    already current."""
    conn = connect(path)
    try:
        conn.executescript(DDL)
        for table, columns in _ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns:
                if name in have:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError:
                    pass            # another process won the race; the column exists either way
        # indexes that depend on a column added above
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS client_users_username "
                     "ON client_users (lower(username)) WHERE username <> ''")
        conn.execute("CREATE INDEX IF NOT EXISTS pm_client_tweet ON post_metrics (client_id, tweet_id)")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()

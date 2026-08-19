"""Configuration — everything comes from the environment / `.env`.

Secrets (app logins, session key) live in `.env`, which is gitignored and never
reaches the browser. See `.env.example` for the full documented list.
"""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# .env loading (tiny parser — avoids another dependency)
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path = None) -> None:
    """Populate os.environ from a KEY=VALUE file. Real env vars always win, so
    `docker run -e FOO=bar` overrides the file."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


load_dotenv()


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name, default=False):
    return (os.environ.get(name, "") or str(int(default))).strip().lower() in (
        "1", "true", "yes", "on")


def _path(name, default):
    p = Path(os.environ.get(name, "") or default)
    return p if p.is_absolute() else (ROOT / p).resolve()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def _parse_users(raw: str) -> dict:
    """'alice:pw1,bob:$2b$12$…' -> {'alice': 'pw1', 'bob': '$2b$12$…'}

    A value beginning with $2 is treated as a bcrypt hash; anything else is a
    plaintext password compared in constant time.
    """
    users = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, _, pw = pair.partition(":")
        name, pw = name.strip(), pw.strip()
        if name and pw:
            users[name] = pw
    return users


def _load_users() -> dict:
    """APP_USERS is the canonical form: 'alice:pw1,bob:pw2'.

    APP_USER + APP_PASS is also accepted for a single account, because
    `gcloud run deploy --set-env-vars` splits on commas — so a multi-user
    APP_USERS needs the awkward '^@^' delimiter syntax, and a one-account deploy
    is much easier to get right with two plain variables.
    """
    users = _parse_users(os.environ.get("APP_USERS", ""))
    single_user = os.environ.get("APP_USER", "").strip()
    single_pass = os.environ.get("APP_PASS", "").strip()
    if single_user and single_pass:
        users.setdefault(single_user, single_pass)
    return users


USERS = _load_users()

# Who sees the setup pages (accounts & sessions, settings, the style designer,
# health and the capture-budget meter). Comma-separated usernames. UNSET means
# everyone is an admin, so an existing single-team deployment changes nothing;
# set it once colleagues who only need to make reports are added.
APP_ADMINS = {u.strip() for u in os.environ.get("APP_ADMINS", "").split(",") if u.strip()}


def is_admin(user: str) -> bool:
    return not APP_ADMINS or (user or "") in APP_ADMINS

# --------------------------------------------------------------------------- #
# Shared X capture account — used to sign in headlessly and regenerate
# sessions/x_state.json when it is missing (free hosts have an ephemeral disk).
# Use a dedicated / throwaway account, never a personal one.
# --------------------------------------------------------------------------- #
X_USERNAME = os.environ.get("X_USERNAME", "").strip()
X_PASSWORD = os.environ.get("X_PASSWORD", "").strip()
X_EMAIL = os.environ.get("X_EMAIL", "").strip()        # for X's "confirm email" step
X_TOTP_SECRET = os.environ.get("X_TOTP_SECRET", "").strip()   # only if 2FA is on

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
SESSION_HOURS = _int("SESSION_HOURS", 12)
COOKIE_SECURE = _bool("COOKIE_SECURE", False)

# --------------------------------------------------------------------------- #
# Capture performance
#
# Chromium runs on this server, so the limit is the box's RAM and cores, not a
# third party's plan. Rule of thumb: one worker per 1–1.5 GB of free RAM
# (a browser costs ~0.5–1 GB).
#
# `WORKERS` is the name used in the deployment docs; CAPTURE_WORKERS is accepted
# as an alias so older .env files keep working.
# --------------------------------------------------------------------------- #
MAX_CONCURRENT_JOBS = max(1, _int("MAX_CONCURRENT_JOBS", 1))
CAPTURE_WORKERS = max(1, _int("WORKERS", _int("CAPTURE_WORKERS", 3)))

# Ceiling for the per-job "Capture speed" picker on the submit form. A user can
# choose any value up to this; the server clamps anything above it.
#
# Keep it honest about the hardware. Browsers only run in parallel if there are
# CORES to run them on — Chromium spends the capture decoding images and video,
# so on a 1-vCPU box three browsers take turns on one core instead of running
# side by side, and the wall-clock barely moves while the RAM cost is real.
# Raise this when you add vCPUs, not before. RAM is the second limit:
# MAX_CONCURRENT_JOBS x MAX_WORKERS browsers at ~0.5-1 GB each.
def _cores() -> int:
    """vCPUs this process may actually use, honouring a container's cpu quota.

    `os.cpu_count()` reports the HOST's cores inside Docker, which on a shared
    VPS is a number the container will never get. cgroup v2 states the real
    quota, so it is read first."""
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if raw[0] != "max":
            return max(1, int(int(raw[0]) / int(raw[1])))
    except Exception:
        pass
    try:                                        # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return max(1, quota // period)
    except Exception:
        pass
    return max(1, os.cpu_count() or 1)


def _available_gb() -> float:
    """RAM this box can still hand out, in GB. 0.0 when it cannot be read."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 0.0


CORES = _cores()
AVAILABLE_GB = _available_gb()

# The honest ceiling for this box, and the reason the "Capture speed" picker
# stopped offering 4 browsers on a 1-vCPU server.
#
# THIS IS WHY A 1232-LINK RUN SHOWED 0 CAPTURED. The old default was the
# constant 4, on every machine, so the form offered four browsers to a 1-core
# VPS and the four Chromiums took turns on that one core — which is exactly what
# the comment above predicted, and which looks from the outside like a hang
# rather than like contention.
#
# Cores + 1, not cores: a capture spends real time waiting on the network (the
# page load, the media settle, the pacing sleep) with the CPU idle, so one
# browser more than there are cores fills those gaps. Two more does not; it just
# adds context switching to a core that is already the bottleneck.
#
# RAM is the second gate at ~1.2 GB per browser, and the floor is 1 — a box that
# cannot be measured still runs one browser rather than zero.
def _hardware_ceiling() -> int:
    by_cpu = CORES + 1
    by_ram = int(AVAILABLE_GB / 1.2) if AVAILABLE_GB else by_cpu
    return max(1, min(by_cpu, by_ram))


HARDWARE_MAX_WORKERS = _hardware_ceiling()

# An explicit MAX_WORKERS in the environment still wins — the hardware number is
# a better DEFAULT, not a cap on someone who has measured their own box. It is
# reported on the Server settings page either way, so an override that is too
# ambitious for the hardware is visible rather than mysterious.
MAX_WORKERS = max(CAPTURE_WORKERS, _int("MAX_WORKERS", 0) or HARDWARE_MAX_WORKERS)

# The Influencer report defaults to one browser: it looks up each author's
# follower count once and caches it, but that cache lives in the worker PROCESS,
# so a second worker re-fetches the same profiles. Raise it if you have the
# cores and the accounts rarely repeat.
INFLUENCER_WORKERS = max(1, _int("INFLUENCER_WORKERS", 1))

JOB_TIMEOUT_MINUTES = max(1, _int("JOB_TIMEOUT_MINUTES", 90))

# How many posts one capture account can take in a day before the session starts
# to rot. Not a guess: measured on 2026-08-03 at ~320 captures, after which retry
# counts, frame heights and parent-losses all degraded together without a single
# error being raised (RULEBOOK rule 21). The dashboard meter reads this so the
# ceiling is visible BEFORE a run, not discovered afterwards in a bad report.
# It is a warning line, not an enforced quota — raise it only after someone
# measures a better number on the account you are actually using.
DAILY_CAPTURE_BUDGET = max(1, _int("DAILY_CAPTURE_BUDGET", 320))

# Grok (xAI) — optional. Used by the smart sheet reader as a second opinion
# on column names, and by Grok Studio later. Empty = every AI feature off.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4").strip() or "grok-4"

# Sheet sources (v3): how often the sync loop looks at every project's sheets.
SHEET_SYNC_MINUTES = max(1, _int("SHEET_SYNC_MINUTES", 10))

# Upload limits
MAX_UPLOAD_MB = max(1, _int("MAX_UPLOAD_MB", 5))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
# 0 = no limit (v3 default). A positive number caps links per job.
MAX_LINKS = max(0, _int("MAX_LINKS", 0))

# Retention
RETENTION_DAYS = max(1, _int("RETENTION_DAYS", 7))
MAX_DATA_GB = max(1, _int("MAX_DATA_GB", 5))

# queue (always-on host) | inline (scale-to-zero host)
EXECUTION_MODE = (os.environ.get("EXECUTION_MODE", "queue") or "queue").strip().lower()

# Paths
DATA_DIR = _path("DATA_DIR", "data")
SESSIONS_DIR = _path("SESSIONS_DIR", "sessions")
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "jobs.db"
# Report styles designed in the web app. Runtime state, not code — it lives
# beside the jobs so a `docker compose up --build` cannot throw it away.
USER_PROFILES_DIR = DATA_DIR / "profiles"
X_STATE_FILE = SESSIONS_DIR / "x_state.json"

PORT = _int("PORT", 8000)

# Login rate limiting
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_WINDOW_MINUTES = _int("LOGIN_WINDOW_MINUTES", 15)

# Legacy constant: the built-in slugs. The authoritative list — built-ins PLUS
# every valid profile — is webapp/report_types.py, which is what the API and the
# form use. Kept so nothing that imported this breaks.
REPORT_TYPES = ("twitter", "influencer")


def startup_warnings() -> list:
    """Misconfiguration the admin should see in the log at boot."""
    warn = []
    if not USERS:
        warn.append("No app logins configured — nobody can log in. Set "
                    "APP_USERS=admin:somepassword (or APP_USER + APP_PASS).")
    if EXECUTION_MODE == "inline" and JOB_TIMEOUT_MINUTES >= 60:
        warn.append(f"JOB_TIMEOUT_MINUTES={JOB_TIMEOUT_MINUTES} but a "
                    "scale-to-zero host caps a request at 60 minutes — the "
                    "host will cut the job off first. Use 50 or less.")
    if any(pw in ("change-me-now", "changeme", "password") for pw in USERS.values()):
        warn.append("APP_USERS still contains a default password — change it.")
    if not SESSION_SECRET or SESSION_SECRET.startswith("change-me"):
        warn.append("SESSION_SECRET is unset or default — sessions will not "
                    "survive a restart and are not secure. Generate one with "
                    "`python -c \"import secrets;print(secrets.token_urlsafe(48))\"`.")
    if not X_STATE_FILE.exists() and not (X_USERNAME and X_PASSWORD):
        warn.append(f"No X login cookie at {X_STATE_FILE} and no X_USERNAME / "
                    "X_PASSWORD to sign in with — captures will hit login "
                    "walls. Either set those in .env, or run "
                    "`python save_login.py x` on your own machine and copy the "
                    "file onto the server.")
    if X_TOTP_SECRET and not (X_USERNAME and X_PASSWORD):
        warn.append("X_TOTP_SECRET is set but X_USERNAME / X_PASSWORD are not — "
                    "auto sign-in is disabled.")
    if EXECUTION_MODE not in ("queue", "inline"):
        warn.append(f"EXECUTION_MODE={EXECUTION_MODE!r} is not 'queue' or "
                    "'inline' — falling back to 'queue'.")
    return warn


def effective_session_secret() -> str:
    """The real secret, or an ephemeral one so dev still works (with a warning
    already emitted by startup_warnings)."""
    return SESSION_SECRET or secrets.token_urlsafe(48)


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    USER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def public_settings() -> dict:
    """The effective, NON-secret configuration, for the Settings page.
    Deliberately a whitelist: nothing here can ever leak a credential."""
    return {
        "Execution mode": EXECUTION_MODE,
        "Default browsers per job (WORKERS)": CAPTURE_WORKERS,
        "Capture speed ceiling (MAX_WORKERS)": MAX_WORKERS,
        "This box": (f"{CORES} vCPU" +
                     (f", {AVAILABLE_GB:.1f} GB free" if AVAILABLE_GB else "")),
        "Browsers this box can really run": (
            f"{HARDWARE_MAX_WORKERS}" +
            ("" if MAX_WORKERS <= HARDWARE_MAX_WORKERS else
             f" — MAX_WORKERS is set to {MAX_WORKERS}, which is above it; "
             f"the extra browsers will take turns on the same core(s) and the "
             f"run will not get faster")),
        "Influencer report browsers (INFLUENCER_WORKERS)": INFLUENCER_WORKERS,
        "Reports running at once (MAX_CONCURRENT_JOBS)": MAX_CONCURRENT_JOBS,
        "Links per report (MAX_LINKS)": MAX_LINKS or "unlimited",
        "Upload size limit": f"{MAX_UPLOAD_MB} MB",
        "Job time limit": f"{JOB_TIMEOUT_MINUTES} min",
        "Reports kept for": f"{RETENTION_DAYS} day(s)",
        "Disk cap for job data": f"{MAX_DATA_GB} GB",
        "Daily capture budget (DAILY_CAPTURE_BUDGET)": DAILY_CAPTURE_BUDGET,
        "Login session length": f"{SESSION_HOURS} h",
        "Secure cookies (HTTPS)": "on" if COOKIE_SECURE else "off",
        "X auto sign-in": "configured" if (X_USERNAME and X_PASSWORD) else "off",
        "Data directory": str(DATA_DIR),
        "Sessions directory": str(SESSIONS_DIR),
    }

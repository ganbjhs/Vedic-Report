"""Run one report job: build an isolated working dir, invoke the pipeline as a
subprocess, follow its progress, publish the artifacts.

ISOLATION — why we copy the code instead of just setting cwd
------------------------------------------------------------
`src/run_report.py` and `src/report_builder.py` both anchor their output with

    ROOT = Path(__file__).resolve().parents[1]

That is derived from where the *source file physically lives*, not from the
current working directory — and `.resolve()` collapses symlinks, so symlinking
`src/` into a job folder would resolve straight back to the original and every
concurrent job would write into the same `reports/`.

So each job gets its own physical copy of the code tree (~90 KB of .py files;
single-digit milliseconds). `sessions/` stays a symlink to the one real cookie
file, because `x_storage_state()` only does `ROOT / "sessions" / "x_state.json"`
with no `.resolve()`.

Result: concurrent jobs cannot see or overwrite each other's output, and not one
line of the frozen pipeline had to change.
"""
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from .. import config, report_types, x_login
from . import store

# Code copied into each job's working directory.
_CODE_ITEMS = ("run.py", "src", "influencer", "profiles", "facebook", "instagram",
               "metrics")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "reports",
                                 "sessions")

# Live handles for cancellation, keyed by job id.
_RUNNING = {}
_RUNNING_LOCK = threading.Lock()


class JobFailed(Exception):
    pass


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def app_dir(job_id: str) -> Path:
    return job_dir(job_id) / "app"


def out_dir(job_id: str) -> Path:
    return job_dir(job_id) / "out"


def log_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.log"


# --------------------------------------------------------------------------- #
# Working directory
# --------------------------------------------------------------------------- #
def build_job_dir(job_id: str, rows: list, upload_bytes: bytes,
                  upload_name: str) -> Path:
    """Create data/jobs/<id>/ with a private copy of the code and the input."""
    from .. import uploads

    jd = job_dir(job_id)
    app = app_dir(jd.name)
    if jd.exists():
        shutil.rmtree(jd, ignore_errors=True)
    app.mkdir(parents=True, exist_ok=True)
    out_dir(job_id).mkdir(parents=True, exist_ok=True)

    for item in _CODE_ITEMS:
        src = config.ROOT / item
        if not src.exists():
            continue
        dst = app / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_IGNORE)
        else:
            shutil.copy2(src, dst)

    # A style designed in the app lives under DATA_DIR, outside the code tree.
    # Copy it (and any user style it extends) into the job's private registry,
    # so the subprocess finds it through the normal path and a later edit or
    # delete cannot change a job that is already running (rule 2, in spirit).
    _copy_user_profiles(job_id, app)

    # One shared, read-only login cookie — symlinked, never copied around.
    link = app / "sessions"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(config.SESSIONS_DIR, target_is_directory=True)
    except OSError:
        # Filesystems without symlinks (rare): fall back to copying just the
        # state file so the job can still authenticate.
        link.mkdir(parents=True, exist_ok=True)
        if config.X_STATE_FILE.exists():
            shutil.copy2(config.X_STATE_FILE, link / "x_state.json")

    # Keep the original upload for troubleshooting (name is display-only).
    up = jd / "upload"
    up.mkdir(parents=True, exist_ok=True)
    (up / uploads.safe_upload_name(upload_name)).write_bytes(upload_bytes)

    uploads.write_canonical_xlsx(rows, app / "input.xlsx")
    # The rows as the reader understood them, kept OUTSIDE app/ so the pipeline
    # never sees them. `_fetch_metrics` re-reads this, fills in the engagement
    # numbers X can tell us, and writes input.xlsx again — which is the whole
    # reason the enrichment needs no change to any builder: a style that prints
    # a metric column prints the filled-in one exactly as it prints a typed one.
    try:
        (jd / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
    except (OSError, TypeError) as e:                    # rule 17: never silent
        print(f"[runner] could not save rows.json for {job_id}: {e}", flush=True)
    return jd


def _copy_user_profiles(job_id: str, app: Path) -> None:
    job = store.get(job_id)
    slug = (job or {}).get("report_type") or ""
    dest_dir = app / "profiles" / "registry"
    seen = set()
    while slug and slug not in seen:
        seen.add(slug)
        src = config.USER_PROFILES_DIR / f"{slug}.json"
        if not src.is_file() or (dest_dir / src.name).exists():
            return                       # shipped profile, or already copied
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / src.name)
        assets = config.USER_PROFILES_DIR / "assets" / slug
        if assets.is_dir():                 # designed-page templates
            shutil.copytree(assets, dest_dir / "assets" / slug, dirs_exist_ok=True)
        try:
            slug = json.loads(src.read_text()).get("extends") or ""
        except ValueError:
            return


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #
def build_command(report_type: str, title: str, date: str,
                  keep_engagement: bool = False, workers: int = 0,
                  outputs=None, fast: bool = False) -> list:
    """The exact CLI invocation, identical in shape to what you run by hand.

    `--no-date` is what makes the document header read exactly what the user
    typed in "Report / File Name" and nothing else. `--date` is still passed so
    the invocation stays reproducible by hand; the switch simply keeps it out of
    the header.

    Which entrypoint runs, whether `--keep-engagement` is offered and whether
    the worker picker applies all come from `report_types`, never from the slug.
    The capability flags encode the reasons:

      * `allows_keep_engagement` is False for the influencer report because its
        capture already keeps likes and reposts in frame, and False for profiles
        because the profile itself declares the crop — offering it would promise
        a choice that is not one.
      * `allows_worker_choice` is False for the influencer report because its
        follower-count cache lives in the worker PROCESS, so a second worker
        re-fetches the same profiles (RULEBOOK rule 12). Because it is a flag
        rather than a slug test, a crafted POST cannot override it.

    0 falls back to the server default for the type. The value is clamped to
    MAX_WORKERS here as well as at the API boundary, because this function is
    also reachable from a stored job record, and one browser too many is an OOM
    kill, not an error message.

    `outputs` is the user's tick boxes, and it reaches the pipeline for PROFILE
    types only. The two built-ins run frozen entrypoints that take no such flag
    (rule 1), so for them the choice is applied in `publish()` — their builders
    still write both files and only the wanted ones are kept and offered. That
    asymmetry is deliberate: a format choice is not worth an edit to `run.py`.
    """
    rt = report_types.get(report_type)
    if rt is None:
        # Unknown slug. Refusing beats guessing: the previous code was
        # `influencer = report_type != "twitter"`, so ANY unrecognised slug
        # silently ran the influencer report (docs/profile-engine.md §7).
        raise JobFailed(
            f"Unknown report type {report_type!r}. Known: "
            f"{', '.join(report_types.slugs())}")

    if rt.allows_worker_choice:
        workers = (min(workers, config.MAX_WORKERS) if workers > 0
                   else rt.default_workers())
    else:
        workers = rt.default_workers()

    cmd = [sys.executable, "-u", *rt.argv, "input.xlsx",
           "--title", title,
           "--date", date,
           "--no-date",
           "--workers", str(workers)]
    if keep_engagement and rt.allows_keep_engagement:
        cmd.append("--keep-engagement")
    # Gated on the capability, never the slug: a profile runner does not take
    # this switch and would fail the job on an unrecognised argument.
    if fast and rt.allows_fast:
        cmd.append("--fast")
    wanted = report_types.clean_outputs(report_type, outputs)
    if not rt.builtin and set(wanted) != set(rt.outputs):
        cmd += ["--outputs", ",".join(wanted)]
    return cmd


# --------------------------------------------------------------------------- #
# Progress parsing — reads the pipeline's own stdout, no pipeline changes
# --------------------------------------------------------------------------- #
_RE_TOTAL = re.compile(r"^\[runner\]\s+(\d+)\s+X link\(s\) loaded")
_RE_WORKERS = re.compile(r"^\[runner\]\s+capturing with (\d+) parallel worker")
_RE_RETRY = re.compile(r"^\[runner\]\s+retrying (\d+) link")
_RE_QUALITY = re.compile(r"^\[quality\]\s+recapturing (\d+)")
_RE_BLOCKED = re.compile(r"^\[quality\]\s+dropping (\d+) shot")
# "dropped", not "dropping" — deliberately distinct from _RE_BLOCKED above, or
# a parent loss would be reported to the user as a stuck X dialog.
_RE_PARENT_LOST = re.compile(r"^\[quality\]\s+dropped (\d+) shot\(s\) whose parent")
_RE_TOO_SMALL = re.compile(r"^\[quality\]\s+dropped (\d+) shot\(s\) too small")
_RE_CROPPED = re.compile(r"^\[quality\]\s+(\d+) shot\(s\) may be missing")
_RE_VERIFY = re.compile(r"^\[verify\]\s+(\d+)/(\d+) links produced")
_RE_RESULT = re.compile(r"^\s+\[x\]\s+(\S+)\s+(.*)$")
_RE_SKIPPED = re.compile(r"^\[input\]\s+skipped (\d+) non-X link")
_RE_NO_SESSION = re.compile(r"^\[runner\]\s+NO saved X session")
_RE_METRICS = re.compile(r"^\[metrics\]\s+(\d+) post\(s\) had at least one metric")
_RE_WROTE = re.compile(r"^\[report\]\s+wrote\s+(.+?)\s+\(")


class _Progress:
    """Translates pipeline stdout into job state + an activity log for the UI."""

    def __init__(self, job_id: str, total: int):
        self.job_id = job_id
        self.total = total
        self.done = 0
        self.phase = "Starting the browser"
        self.login_wall = False
        self._last_push = 0.0
        # A long run used to show "0 / 1232 posts captured" and a phase that did
        # not move for minutes, because the pipeline's per-link lines are only
        # printed after capture ENDS and the bar is driven by files on disk. A
        # healthy 1232-link job is indistinguishable from a hung one for its
        # first minute, and the reasonable thing to do with a hung job is cancel
        # it — which is what happened. These two fields drive a heartbeat that
        # says what is going on while nothing has landed yet.
        self._capture_started = 0.0
        self._warmed = False

    def note(self, message: str, level: str = "info") -> None:
        store.append_activity(self.job_id, message, level)

    def _push(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_push > 1.0:
            self._last_push = now
            store.update(self.job_id, phase=self.phase, done=self.done,
                         total=self.total)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self._push(force=True)

    def observe_shots(self, count: int) -> None:
        """Live count from the screenshots folder while capture is running."""
        if count > self.done:
            self.done = min(count, self.total or count)
            self._push()

    def heartbeat(self) -> None:
        """Keep the phase line honest between screenshots.

        Called on a timer, not on an event, precisely because the silent stretch
        is the problem: before the first PNG lands there is no event to hang a
        message on. Once shots are landing it reports the real rate and a
        finish estimate from THIS run's measured pace, not from an assumption.
        """
        if not self._capture_started:
            return
        elapsed = time.time() - self._capture_started
        if self.done <= 0:
            if elapsed > 45 and not self._warmed:
                self._warmed = True
                self.note("Browsers are up and loading the first posts. On a "
                          "long list the first screenshots take a minute or so "
                          "to appear — the job is running.")
            self.set_phase("Capturing posts — loading the first ones "
                           f"({int(elapsed)}s)")
            return
        rate = self.done / elapsed * 60.0            # posts per minute
        left = max(0, (self.total or self.done) - self.done)
        eta = left / rate if rate > 0 else 0
        self.set_phase(f"Capturing posts — {self.done}/{self.total} · "
                       f"{rate:.0f}/min · about {_human_minutes(eta)} left")

    def line(self, text: str) -> None:
        m = _RE_TOTAL.match(text)
        if m:
            self.total = int(m.group(1))
            self.set_phase("Capturing posts")
            self.note(f"{self.total} X link(s) loaded from the uploaded file.")
            return
        m = _RE_SKIPPED.match(text)
        if m:
            self.note(f"{m.group(1)} non-X link(s) in the file were skipped.",
                      "warn")
            return
        if _RE_NO_SESSION.match(text):
            self.login_wall = True
            self.note("No saved X login found on the server — posts will hit a "
                      "login wall. An admin needs to upload sessions/x_state.json.",
                      "error")
            return
        m = _RE_WORKERS.match(text)
        if m:
            self._capture_started = time.time()
            self.set_phase(f"Capturing posts ({m.group(1)} parallel browser(s))")
            return
        m = _RE_RETRY.match(text)
        if m:
            self.set_phase("Retrying links that failed the first pass")
            self.note(f"Retrying {m.group(1)} link(s) that did not capture cleanly.",
                      "warn")
            return
        m = _RE_QUALITY.match(text)
        if m:
            self.set_phase("Re-capturing low-quality screenshots")
            self.note(f"Re-capturing {m.group(1)} screenshot(s) that came out "
                      "blank, black, half-loaded or covered by an X dialog.",
                      "warn")
            return
        m = _RE_BLOCKED.match(text)
        if m:
            self.note(f"{m.group(1)} post(s) still had an X dialog on top after "
                      "every retake — left out rather than shown as a popup.",
                      "warn")
            return
        m = _RE_PARENT_LOST.match(text)
        if m:
            self.note(f"{m.group(1)} reply/replies could not be captured with "
                      "their parent post and were left out — a reply without "
                      "the post it answers is misleading evidence.", "warn")
            return
        m = _RE_TOO_SMALL.match(text)
        if m:
            self.note(f"{m.group(1)} capture(s) came out too small to contain a "
                      "post and were left out — usually a deleted post whose "
                      "page still shows the surrounding conversation.", "warn")
            return
        m = _RE_CROPPED.match(text)
        if m:
            self.note(f"{m.group(1)} screenshot(s) may not show the whole post "
                      "and its reply — check them in screenshots.zip.", "warn")
            return
        m = _RE_VERIFY.match(text)
        if m:
            good, total = int(m.group(1)), int(m.group(2))
            self.done, self.total = good, total
            self.set_phase("Building the document")
            level = "info" if good == total else "warn"
            self.note(f"Verified: {good}/{total} links produced a clean screenshot.",
                      level)
            return
        m = _RE_RESULT.match(text)
        if m and m.group(1) != "ok":
            status_txt, who = m.group(1), m.group(2).strip()
            self.note(f"Could not capture {who or 'a post'} — {status_txt}.", "warn")
            return
        m = _RE_METRICS.match(text)
        if m:
            self.note(f"{m.group(1)} post(s) had at least one engagement metric "
                      "unavailable — those show as — in the report.", "warn")
            return
        m = _RE_WROTE.match(text)
        if m:
            self.set_phase("Packaging downloads")
            return


def _human_minutes(minutes: float) -> str:
    if minutes < 1:
        return "a minute"
    if minutes < 90:
        return f"{int(round(minutes))} min"
    return f"{minutes / 60:.1f} h"


def _shot_count(app: Path) -> int:
    shots = app / "reports" / "screenshots"
    try:
        return sum(1 for p in shots.iterdir() if p.suffix.lower() == ".png")
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #
def _zip_screenshots(app: Path, dest: Path) -> bool:
    shots = app / "reports" / "screenshots"
    files = sorted(p for p in shots.glob("*.png")) if shots.exists() else []
    if not files:
        return False
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=f"screenshots/{p.name}")
    return True


def _read_results(app: Path) -> list:
    f = app / "reports" / "results.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except (ValueError, OSError):
        return []


_STATUS_REASON = {
    "login_wall": "the site asked for a login before showing this post — for X "
                  "the server's session may have expired; on Facebook the post "
                  "is not public",
    "not_found": "post unavailable, deleted, protected or suspended",
    "age_restricted": "X age-restricted this post and only accepts age "
                      "verification through its mobile app, so the content "
                      "cannot be shown in a desktop capture",
    "overlay_blocked": "a dialog stayed on top of the post through every "
                       "retake, so the screenshot showed the popup instead",
    "parent_lost": "this post is a reply and its parent post could not be "
                   "captured, so the screenshot would have shown the reply "
                   "without the post it answers",
    "too_small": "the capture came out too small to contain a post — usually a "
                 "deleted post, or one whose author was suspended, where X "
                 "still renders the surrounding conversation",
}


def _skipped_from_results(results: list) -> list:
    """Links that did NOT make it into the document, with a plain-English why.

    The document itself drops them (matching the Twitter report's behaviour);
    this list is what the UI's activity panel shows.
    """
    skipped = []
    for r in results:
        status_txt = r.get("status") or "error"
        shot = r.get("screenshot")
        has_shot = bool(shot) and Path(shot).exists() and Path(shot).stat().st_size > 0
        if status_txt == "ok" and has_shot:
            continue
        if status_txt.startswith("error"):
            reason = status_txt[6:].strip(": ") or "capture error"
        else:
            reason = _STATUS_REASON.get(status_txt,
                                        "screenshot did not render" if status_txt == "ok"
                                        else status_txt)
        skipped.append({
            "account": r.get("account_name") or r.get("handle") or "",
            "link": r.get("post_link") or r.get("url") or "",
            "reason": reason,
        })
    return skipped


# Documents a job can publish. `xlsx` is the global data export, not a report
# format, so it is never filtered by the user's format choice.
_REPORT_EXTS = ("pdf", "docx", "pptx")


def publish(job_id: str, app: Path, stem: str, wanted=None,
            metrics_csv: Path = None) -> dict:
    """Move the pipeline's output into out/ under the user's chosen name.

    `wanted` — the formats the user ticked. For a profile type the pipeline was
    already told, so nothing else is there to skip; for the two built-ins it is
    the whole mechanism: their frozen builders always write PDF and DOCX, and
    an unticked format is simply not published, not offered and not downloadable.
    """
    out = out_dir(job_id)
    out.mkdir(parents=True, exist_ok=True)
    reports = app / "reports"
    artifacts = {}
    keep = {str(w).lower() for w in (wanted or ())}

    for ext in (*_REPORT_EXTS, "xlsx"):
        if keep and ext in _REPORT_EXTS and ext not in keep:
            continue
        produced = sorted(reports.glob(f"*.{ext}"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        if produced:
            dest = out / f"{stem}.{ext}"
            shutil.copy2(produced[0], dest)
            artifacts[ext] = dest.name

    zip_dest = out / f"{stem}_screenshots.zip"
    if _zip_screenshots(app, zip_dest):
        artifacts["zip"] = zip_dest.name

    # The engagement numbers as READ, exact and unrounded — the report shows
    # X's compact form, and someone will want to add them up.
    if metrics_csv is not None and Path(metrics_csv).is_file():
        dest = out / f"{stem}_metrics.csv"
        shutil.copy2(metrics_csv, dest)
        artifacts["csv"] = dest.name

    return artifacts


# --------------------------------------------------------------------------- #
# Engagement metadata — read the numbers off the posts, before the document
# --------------------------------------------------------------------------- #
# What X can tell us -> the sheet's own metric keys (profiles/netlinks.py
# METRIC_HEADERS). Views, reach and impressions are ONE number on X: the sheet
# itself heads that column "Reach/views", so all three keys get it rather than
# forcing a choice the platform does not make.
_METRIC_MAP = (("likes", ("like",)),
               ("reposts", ("shares",)),
               ("replies", ("comments",)),
               ("views", ("views", "reach", "impressions")))

_RE_M_READING = re.compile(r"^\[metrics\]\s+reading (\d+) X post")
_RE_M_ONE = re.compile(r"^\[metrics\]\s+(\d+)/(\d+)\s")
_RE_M_NO_SESSION = re.compile(r"^\[metrics\]\s+NO saved X session")
_RE_M_UNREAD = re.compile(r"^\[metrics\]\s+(\d+) post\(s\) could not be opened")
# NOT "had at least one metric …" — that is _RE_METRICS, the influencer
# report's own line. Two regexes matching one sentence is the 'dropping' vs
# 'dropped' trap (rule 20) waiting to happen; see profiles/progress.py.
_RE_M_PARTIAL = re.compile(r"^\[metrics\]\s+(\d+) post\(s\) were missing")


def _fetch_metrics(job_id: str, app: Path, prog: "_Progress") -> Path:
    """Read each X post's engagement numbers and fill in the blanks.

    Runs the reader as its own subprocess in the job's private code copy, for
    the same reason the capture does (rule 2): it is the pipeline's own code,
    isolated per job, and killable with the job. Then the numbers are merged
    into `rows.json` and the canonical sheet is written again.

    ONLY BLANKS ARE FILLED. A number the team typed into the sheet always wins:
    they read theirs from Insights, ours off a public page, and quietly
    replacing a hand-checked figure would be the worst kind of helpful.

    Never fatal. A reader that fails costs the report its filled-in metrics, not
    the report — so every failure is logged and the capture starts regardless.
    Returns the CSV of what was read, or None.
    """
    jd = job_dir(job_id)
    rows_file = jd / "rows.json"
    if not rows_file.exists():
        prog.note("Engagement numbers were requested but this job has no saved "
                  "row list, so there was nothing to fill in.", "warn")
        return None
    try:
        rows = json.loads(rows_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        prog.note(f"Engagement numbers skipped — could not read the row list ({e}).",
                  "warn")
        return None

    prog.set_phase("Reading engagement numbers from X")
    cmd = [sys.executable, "-u", "metrics/x_metrics.py", "input.xlsx",
           "--json", "metrics.json", "--csv", "metrics.csv"]
    log = log_path(job_id).open("a", encoding="utf-8")
    log.write("$ " + " ".join(cmd) + "\n\n")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(app), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            start_new_session=True)
    except OSError as e:
        log.close()
        prog.note(f"Engagement numbers skipped — could not start the reader ({e}).",
                  "warn")
        return None
    _register(job_id, proc)
    total = 0
    try:
        for raw in proc.stdout:
            text = raw.rstrip("\n")
            log.write(raw)
            m = _RE_M_READING.match(text)
            if m:
                total = int(m.group(1))
                prog.note(f"Reading likes, reposts, replies and views from "
                          f"{total} X post(s) before building the report.")
                continue
            m = _RE_M_ONE.match(text)
            if m:
                prog.set_phase(f"Reading engagement numbers ({m.group(1)}/{m.group(2)})")
                continue
            if _RE_M_NO_SESSION.match(text):
                prog.note("No saved X login — engagement numbers were read logged "
                          "out, so view counts will mostly be unavailable.", "warn")
                continue
            m = _RE_M_UNREAD.match(text)
            if m:
                prog.note(f"{m.group(1)} post(s) would not open at all, so they "
                          f"kept whatever the sheet said.", "warn")
                continue
            m = _RE_M_PARTIAL.match(text)
            if m:
                prog.note(f"{m.group(1)} post(s) had at least one number X did "
                          f"not show — those cells were left as they were, not "
                          f"written as 0.", "warn")
                continue
        proc.wait()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        log.close()
        _unregister(job_id)

    read_file = app / "metrics.json"
    if not read_file.exists():
        prog.note("Engagement numbers could not be read — the report will show "
                  "whatever the sheet already had. See the job log.", "warn")
        return None
    try:
        read = json.loads(read_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        prog.note(f"Engagement numbers skipped — unreadable result ({e}).", "warn")
        return None

    filled = _merge_metrics(rows, read)
    if filled:
        from .. import uploads
        try:
            uploads.write_canonical_xlsx(rows, app / "input.xlsx")
            rows_file.write_text(json.dumps(rows), encoding="utf-8")
        except Exception as e:
            prog.note(f"Read the engagement numbers but could not write them "
                      f"into the input sheet ({e}) — the report will show what "
                      f"the sheet already had.", "warn")
            return app / "metrics.csv"
        prog.note(f"Filled in {filled} engagement value(s) the sheet had left "
                  f"blank. Numbers already typed into the sheet were kept.")
    else:
        prog.note("No engagement value was missing from the sheet, so nothing "
                  "was changed. The numbers read from X are in the CSV download.")
    return app / "metrics.csv"


def _merge_metrics(rows: list, read: list) -> int:
    """Fill blank `sheet_metrics` from what the reader saw. Returns how many.

    Values are written in X's own compact form (984, 1.2K, 45K) because that is
    how the platform states them and how they fit the metric pills the deck
    styles draw. The exact integers are never lost — they are in metrics.json
    and in the CSV download beside the report.
    """
    by_link = {}
    for r in read or []:
        if (r or {}).get("status") == "ok":
            by_link[str(r.get("link") or "").strip()] = r
    if not by_link:
        return 0
    filled = 0
    for row in rows:
        got = by_link.get(str(row.get("link") or "").strip())
        if got is None:
            continue
        metrics = dict(row.get("sheet_metrics") or {})
        display = got.get("display") or {}
        for source_key, sheet_keys in _METRIC_MAP:
            if got.get(source_key) is None:
                continue                       # X did not say -> leave the gap
            value = str(display.get(source_key) or "").strip()
            if not value or value == "\u2014":
                continue
            for key in sheet_keys:
                if str(metrics.get(key) or "").strip():
                    continue                   # typed by a human -> untouched
                metrics[key] = value
                filled += 1
        if metrics:
            row["sheet_metrics"] = metrics
    return filled


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #
def _register(job_id: str, proc) -> None:
    with _RUNNING_LOCK:
        _RUNNING[job_id] = proc


def _unregister(job_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.pop(job_id, None)


def cancel(job_id: str) -> bool:
    """Kill a running job's process group (the capture forks worker processes)."""
    with _RUNNING_LOCK:
        proc = _RUNNING.get(job_id)
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            proc.terminate()
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------- #
# The job itself
# --------------------------------------------------------------------------- #
def run_job(job_id: str, on_line=None) -> dict:
    """Execute one job start to finish. Web-framework agnostic, so the same
    function backs both the background queue and the inline (scale-to-zero)
    execution mode.

    `on_line(text)` — optional callback fed every stdout line, used by the
    inline mode to stream progress back over the open HTTP connection.
    """
    job = store.get(job_id)
    if not job:
        raise JobFailed(f"Unknown job {job_id}")
    if job["status"] in store.DONE_STATES:
        return job          # cancelled (or already finished) before its turn came

    app = app_dir(job_id)
    stem = job["name"]
    date = datetime.date.today().strftime("%d-%m-%y")
    keep_engagement = bool(job.get("keep_engagement"))
    outputs = report_types.clean_outputs(job["report_type"],
                                         job.get("outputs") or ())
    cmd = build_command(job["report_type"], job["title"], date, keep_engagement,
                        int(job.get("workers") or 0), outputs,
                        bool(job.get("fast_capture")))

    store.update(job_id, status="running", started_at=time.time(),
                 phase="Checking the X login", error="")
    prog = _Progress(job_id, job.get("total") or job.get("link_count") or 0)
    prog.note(f"Job started — {job['report_type']} report, "
              f"{job.get('link_count', 0)} link(s).")
    rt = report_types.get(job["report_type"])
    if rt is not None and set(outputs) != set(rt.outputs):
        prog.note(f"Building {', '.join(o.upper() for o in outputs)} only — "
                  f"{rt.label} can also produce "
                  f"{', '.join(o.upper() for o in rt.outputs if o not in outputs)}.")
    chosen = int(job.get("workers") or 0)
    if chosen and rt is not None and rt.allows_worker_choice:
        capped = min(chosen, config.MAX_WORKERS)
        # "up to": the pipeline also caps workers at the number of links, so a
        # 4-browser choice on 2 links really runs 2.
        prog.note(f"Capturing with up to {capped} browser(s)" +
                  (f" — {chosen} was above this server's limit of "
                   f"{config.MAX_WORKERS}." if capped < chosen else "."))
    if keep_engagement and rt is not None and rt.allows_keep_engagement:
        prog.note("Screenshots will keep the engagement line (replies, reposts, "
                  "likes, views) — on a comment link, the parent's line and the "
                  "comment's own.")

    # The cookie file lives on an ephemeral disk on free hosts, so make sure a
    # valid X session exists before the capture starts. Cheap when it already
    # does; signs in headlessly when it does not.
    if rt is not None and rt.platform != "x":
        prog.note(f"{rt.platform.title()} capture — public posts, no account "
                  "required.")
    else:
        ok, message = x_login.ensure_session()
        if not ok:
            prog.note(f"X login: {message}", "warn")
        elif "Signed in" in message:
            prog.note("Signed in to X automatically (the saved session was "
                      "missing or expired).")
    # Engagement numbers BEFORE the capture: the merged values go into
    # input.xlsx, which the pipeline is about to read.
    metrics_csv = None
    if bool(job.get("fetch_metrics")):
        if rt is not None and rt.platform not in ("x", "combined"):
            prog.note(f"Engagement numbers are read from X only, and this is a "
                      f"{rt.platform} style — skipped.", "warn")
        else:
            try:
                metrics_csv = _fetch_metrics(job_id, app, prog)
            except Exception as e:              # rule 17: never silent
                prog.note(f"Engagement numbers skipped — the reader failed "
                          f"({e}). The report itself is unaffected.", "warn")
        if (store.get(job_id) or {}).get("status") == "cancelled":
            store.update(job_id, finished_at=time.time(), phase="Cancelled")
            return store.get(job_id)

    prog.set_phase("Starting the browser")

    log = log_path(job_id).open("a", encoding="utf-8")
    log.write("$ " + " ".join(cmd) + "\n\n")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        cmd, cwd=str(app), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        start_new_session=True)          # own process group -> cancellable
    _register(job_id, proc)

    # Watchdog: hard timeout so a wedged browser can't pin a capture slot forever.
    timed_out = {"hit": False}

    def _watchdog():
        deadline = time.time() + config.JOB_TIMEOUT_MINUTES * 60
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(2)
        if proc.poll() is None:
            timed_out["hit"] = True
            cancel(job_id)

    # Live progress: the per-link result lines only print after capture ends, so
    # we count screenshot files on disk to drive the "captured 12 / 40" bar.
    def _shot_poller():
        beat = 0
        while proc.poll() is None:
            prog.observe_shots(_shot_count(app))
            beat += 1
            if beat % 8 == 0:                 # every ~16s
                try:
                    prog.heartbeat()
                except Exception:
                    pass
            time.sleep(2)

    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_shot_poller, daemon=True).start()

    try:
        for raw in proc.stdout:
            text = raw.rstrip("\n")
            log.write(raw)
            prog.line(text)
            if on_line:
                try:
                    on_line(text)
                except Exception:
                    pass
        code = proc.wait()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        log.close()
        _unregister(job_id)

    results = _read_results(app)
    skipped = _skipped_from_results(results)
    artifacts = publish(job_id, app, stem, outputs, metrics_csv)
    captured = len(results) - len(skipped)

    # Success means a DOCUMENT was produced. A failed link still leaves an
    # evidence screenshot behind, so screenshots.zip alone is not success —
    # without this the "every link was unavailable" case would report "done".
    has_document = any(artifacts.get(ext) for ext in _REPORT_EXTS)

    for s in skipped:
        prog.note(f"Not in the report: {s['account'] or s['link']} — {s['reason']}",
                  "warn")

    if any(r.get("status") == "login_wall" for r in results) and (
            rt is None or rt.platform == "x"):
        prog.login_wall = True
        # The cookie exists but X is not honouring it. Drop it so the next job
        # signs in again — but only if the server can sign in; otherwise the
        # hand-uploaded file is all there is and must be kept.
        if x_login.invalidate("captures hit an X login wall"):
            prog.note("Discarded the rejected X session — the next report will "
                      "sign in again.", "warn")

    finished = time.time()
    # A user cancellation already set the final status. The subprocess then exits
    # with SIGTERM (-15), which must NOT be re-reported as a failure.
    if (store.get(job_id) or {}).get("status") == "cancelled":
        store.update(job_id, finished_at=finished, phase="Cancelled",
                     skipped=skipped, artifacts=artifacts, done=captured)
        return store.get(job_id)

    if timed_out["hit"]:
        store.update(job_id, status="failed", finished_at=finished,
                     phase="Timed out", skipped=skipped, artifacts=artifacts,
                     error=f"The job ran longer than {config.JOB_TIMEOUT_MINUTES} "
                           "minutes and was stopped.")
        prog.note("Job stopped: exceeded the time limit.", "error")
    elif code != 0 and not has_document:
        tail = _log_tail(job_id)
        store.update(job_id, status="failed", finished_at=finished,
                     phase="Failed", skipped=skipped, artifacts=artifacts,
                     error=f"The capture process exited with code {code}.\n{tail}")
        prog.note(f"Job failed — process exited with code {code}.", "error")
    elif not has_document:
        reason = (("Every link hit an X login wall — the X session was rejected. "
                   "The server will try to sign in again on the next run; if "
                   "this keeps happening, check the X account credentials on "
                   "the X login page."
                   if x_login.credentials_configured() else
                   "Every link hit an X login wall — the server's saved X "
                   "session has expired and no X account is configured to sign "
                   "in with. See the X login page.")
                  if prog.login_wall else
                  "No link produced a usable screenshot, so there was nothing "
                  "to put in the report. See the list below for why each link "
                  "failed.")
        store.update(job_id, status="failed", finished_at=finished,
                     phase="Failed", done=0, skipped=skipped,
                     artifacts=artifacts, error=reason)
        prog.note(reason, "error")
    else:
        store.update(job_id, status="done", finished_at=finished,
                     phase="Done", done=captured, total=len(results) or prog.total,
                     artifacts=artifacts, skipped=skipped)
        note = f"Report ready — {captured} post(s) included."
        if skipped:
            note += f" {len(skipped)} link(s) could not be captured and were left out."
        prog.note(note, "info")
        if prog.login_wall:
            prog.note("Some posts hit an X login wall — the server's X session "
                      "may be expiring. Consider refreshing it.", "warn")

    return store.get(job_id)


def _log_tail(job_id: str, lines: int = 12) -> str:
    try:
        text = log_path(job_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])

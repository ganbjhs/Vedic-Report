"""The stdout vocabulary the web layer parses. Emit through here, never by hand.

`webapp/jobs/runner.py::_Progress` drives the job page — phase text, the
progress bar, the activity log — by regex-matching the pipeline's stdout. That
makes these strings a **contract**, and an invisible one: reword a `print()` and
the progress bar silently dies with no exception, no failing test and no clue in
the log. The Twitter and Influencer runners hold that contract by having been
written before the web layer and never touched since.

A new runner has no such protection, so it routes every progress line through
the emitters below, and `tests/test_progress_contract.py` asserts each emitter's
output against the **real regexes imported from `webapp/jobs/runner.py`**. Change
either side and a test fails instead of a feature.

Nothing here imports Playwright or touches a file.
"""


def _say(line: str) -> str:
    print(line, flush=True)
    return line


# --------------------------------------------------------------------------- #
# Input  (_RE_SKIPPED)
# --------------------------------------------------------------------------- #
def skipped_non_x(count: int) -> str:
    return _say(f"[input] skipped {count} non-X link(s) — this tool is X-only")


# --------------------------------------------------------------------------- #
# Runner  (_RE_TOTAL, _RE_NO_SESSION, _RE_WORKERS, _RE_RETRY)
# --------------------------------------------------------------------------- #
def total(count: int) -> str:
    """MUST be emitted: it is where the job's progress total comes from."""
    return _say(f"[runner] {count} X link(s) loaded")


def no_session() -> str:
    return _say("[runner] NO saved X session — running logged-out "
                "(run: python src/save_sessions.py x)")


def workers(count: int) -> str:
    return _say(f"[runner] capturing with {count} parallel worker(s)...")


def retrying(count: int) -> str:
    return _say(f"[runner] retrying {count} link(s) sequentially...")


def retry_recovered(fixed: int, total_: int) -> str:
    return _say(f"[runner] retry recovered {fixed}/{total_}")


def wrote_results(path) -> str:
    return _say(f"[runner] wrote {path}")


# --------------------------------------------------------------------------- #
# Quality  (_RE_QUALITY, _RE_BLOCKED, _RE_PARENT_LOST, _RE_CROPPED)
# --------------------------------------------------------------------------- #
def recapturing(count: int) -> str:
    return _say(f"[quality] recapturing {count} low-quality screenshot(s)...")


def recapture_note(account: str, why: str) -> str:
    return _say(f"[quality]   ↻ {account}  ({why})")


def improved(fixed: int, total_: int) -> str:
    return _say(f"[quality] improved {fixed}/{total_}")


def dropping_overlay(count: int) -> str:
    return _say(f"[quality] dropping {count} shot(s) still covered by an X dialog")


def dropped_parent_lost(count: int) -> str:
    """Note 'dropped', not 'dropping' — the web layer distinguishes the two, and
    getting it wrong reports a parent loss to the user as a stuck X dialog."""
    return _say(f"[quality] dropped {count} shot(s) whose parent post "
                "could not be captured")


def maybe_cropped(count: int) -> str:
    return _say(f"[quality] {count} shot(s) may be missing the parent post "
                "or the reply")


# --------------------------------------------------------------------------- #
# Verify / metrics / report  (_RE_VERIFY, _RE_RESULT, _RE_METRICS, _RE_WROTE)
# --------------------------------------------------------------------------- #
def verify(good: int, total_: int) -> str:
    """MUST be emitted: it moves the job to the 'Building the document' phase."""
    return _say(f"[verify] {good}/{total_} links produced a clean screenshot")


def verify_failure(account: str, why: str, link: str) -> str:
    return _say(f"[verify]   ✗ {account}  ({why})  {link}")


def result_line(status: str, handle: str, account: str) -> str:
    """One per link. The web layer turns any non-'ok' status into an activity
    warning, so the status token must not contain a space."""
    return _say(f"  [x] {status:12} {handle or ''}  {account}")


def metrics_missing(count: int) -> str:
    return _say(f"[metrics] {count} post(s) had at least one metric unavailable "
                "(shown as — in the report)")


def wrote(path, mb: float) -> str:
    """MUST match _RE_WROTE — it moves the job to 'Packaging downloads'."""
    return _say(f"[report] wrote {path}  ({mb} MB)")

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


def dropped_too_small(count: int) -> str:
    return _say(f"[quality] dropped {count} shot(s) too small to contain a post")


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


# --------------------------------------------------------------------------- #
# Engagement metadata  (_RE_M_READING, _RE_M_ONE, _RE_M_NO_SESSION,
#                       _RE_M_UNREAD, _RE_M_PARTIAL)
#
# `metrics/x_metrics.py` reads likes / reposts / replies / views off the posts
# before the document is built. It runs as its own subprocess, so its stdout is
# the same kind of invisible contract everything above is — and it says so here
# rather than in its own file, where the contract test could not see it.
#
# NOTE the wording of `metrics_partial`. The obvious phrasing, "had at least one
# metric unavailable", is already `metrics_missing`'s — the influencer report's
# line — and the two would then be one regex wearing two hats, which is how the
# 'dropping' vs 'dropped' bug happened. Different fact, different words.
# --------------------------------------------------------------------------- #
def metrics_reading(count: int) -> str:
    """MUST be emitted: it is where the reader's own progress count starts."""
    return _say(f"[metrics] reading {count} X post(s) for engagement numbers")


def metrics_one(n: int, total_: int, status: str, likes: str, reposts: str,
                replies: str, views: str, link: str) -> str:
    """One per post, as it is read."""
    return _say(f"[metrics] {n}/{total_} {status} · {likes} likes · "
                f"{reposts} reposts · {replies} replies · {views} views · {link}")


def metrics_no_session() -> str:
    return _say("[metrics] NO saved X session — reading logged out, so view "
                "counts will usually be unavailable")


def metrics_unread(count: int) -> str:
    return _say(f"[metrics] {count} post(s) could not be opened at all")


def metrics_partial(count: int) -> str:
    return _say(f"[metrics] {count} post(s) were missing a number X did not "
                "show — left blank, never written as 0")

#!/usr/bin/env python3
"""The stdout contract, made testable.

`webapp/jobs/runner.py::_Progress` parses pipeline stdout with literal regexes.
That coupling is invisible: reword a print() and the job page's progress bar
dies silently — no exception, no failing test, nothing in the log.

This suite imports the REAL regexes from the web layer and asserts that every
emitter in `profiles/progress.py` matches the one it is supposed to, that the
captured groups carry the right numbers, and that emitters do NOT match each
other's regexes (the "dropping" vs "dropped" trap). It also asserts that the
frozen runners' own wording still matches, so this catches drift on either side.

Zero captures.

    .venv/bin/python profiles/tests/test_progress_contract.py
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "profiles"))

import progress as P                              # noqa: E402
from webapp.jobs import runner as RN              # noqa: E402

FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          ("" if ok else f": got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def emitted(fn, *a, **kw):
    """Call an emitter, returning what it printed (and proving it prints)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = fn(*a, **kw)
    printed = buf.getvalue().rstrip("\n")
    assert printed == ret, f"{fn.__name__} returned {ret!r} but printed {printed!r}"
    return printed


def matches(regex, line, groups=None):
    m = regex.match(line)
    if not m:
        return False
    if groups is None:
        return True
    return tuple(m.groups()) == tuple(groups)


# --------------------------------------------------------------------------- #
print("\n1. every emitter matches its regex, with the right captured groups")

CASES = [
    ("skipped_non_x",       lambda: emitted(P.skipped_non_x, 3),      RN._RE_SKIPPED,  ("3",)),
    ("total",               lambda: emitted(P.total, 42),             RN._RE_TOTAL,    ("42",)),
    ("no_session",          lambda: emitted(P.no_session),            RN._RE_NO_SESSION, None),
    ("workers",             lambda: emitted(P.workers, 3),            RN._RE_WORKERS,  ("3",)),
    ("retrying",            lambda: emitted(P.retrying, 7),           RN._RE_RETRY,    ("7",)),
    ("recapturing",         lambda: emitted(P.recapturing, 2),        RN._RE_QUALITY,  ("2",)),
    ("dropping_overlay",    lambda: emitted(P.dropping_overlay, 5),   RN._RE_BLOCKED,  ("5",)),
    ("dropped_parent_lost", lambda: emitted(P.dropped_parent_lost, 4), RN._RE_PARENT_LOST, ("4",)),
    ("dropped_too_small",   lambda: emitted(P.dropped_too_small, 6),   RN._RE_TOO_SMALL, ("6",)),
    ("maybe_cropped",       lambda: emitted(P.maybe_cropped, 6),      RN._RE_CROPPED,  ("6",)),
    ("verify",              lambda: emitted(P.verify, 18, 20),        RN._RE_VERIFY,   ("18", "20")),
    ("metrics_missing",     lambda: emitted(P.metrics_missing, 9),    RN._RE_METRICS,  ("9",)),
    ("wrote",               lambda: emitted(P.wrote, "/tmp/a.pdf", 1.3), RN._RE_WROTE, ("/tmp/a.pdf",)),
    # metrics/x_metrics.py — the engagement reader that runs before the capture
    ("metrics_reading",     lambda: emitted(P.metrics_reading, 12),    RN._RE_M_READING, ("12",)),
    ("metrics_one",         lambda: emitted(P.metrics_one, 3, 12, "ok", "45", "6",
                                            "2", "7.8K", "https://x.com/a/status/1"),
                                                                      RN._RE_M_ONE, ("3", "12")),
    ("metrics_no_session",  lambda: emitted(P.metrics_no_session),     RN._RE_M_NO_SESSION, None),
    ("metrics_unread",      lambda: emitted(P.metrics_unread, 4),      RN._RE_M_UNREAD, ("4",)),
    ("metrics_partial",     lambda: emitted(P.metrics_partial, 5),     RN._RE_M_PARTIAL, ("5",)),
]
for name, make, regex, groups in CASES:
    check(f"{name} -> {regex.pattern[:34]}...", matches(regex, make(), groups))

line = emitted(P.result_line, "parent_lost", "@who", "Some Account")
check("result_line -> _RE_RESULT",
      matches(RN._RE_RESULT, line, ("parent_lost", "@who  Some Account")))

# --------------------------------------------------------------------------- #
print("\n2. the 'dropping' vs 'dropped' trap stays shut")
overlay = emitted(P.dropping_overlay, 5)
parent = emitted(P.dropped_parent_lost, 4)
check("overlay line does NOT match the parent regex",
      matches(RN._RE_PARENT_LOST, overlay), False)
check("parent line does NOT match the overlay regex",
      matches(RN._RE_BLOCKED, parent), False)
check("parent line does NOT match the cropped regex",
      matches(RN._RE_CROPPED, parent), False)
small = emitted(P.dropped_too_small, 6)
check("too-small line does NOT match the parent regex",
      matches(RN._RE_PARENT_LOST, small), False)
check("too-small line does NOT match the overlay regex",
      matches(RN._RE_BLOCKED, small), False)

# --------------------------------------------------------------------------- #
print("\n3. no emitter accidentally matches a DIFFERENT regex")
ALL_RE = {n: getattr(RN, n) for n in dir(RN) if n.startswith("_RE_")}
EXPECTED = {name: regex for name, _m, regex, _g in CASES}
EXPECTED["result_line"] = RN._RE_RESULT
for name, make, _regex, _g in CASES + [("result_line", lambda: line, None, None)]:
    text = make()
    hits = {rn for rn, rx in ALL_RE.items() if rx.match(text)}
    want = {rn for rn, rx in ALL_RE.items() if rx is EXPECTED[name]}
    check(f"{name} matches exactly {sorted(want)}", hits, want)

# --------------------------------------------------------------------------- #
print("\n4. the FROZEN runners' own wording still matches (drift on their side)")
FROZEN_LINES = [
    ("[input] skipped 2 non-X link(s) — this tool is X-only", RN._RE_SKIPPED),
    ("[runner] 55 X link(s) loaded", RN._RE_TOTAL),
    ("[runner] NO saved X session — running logged-out", RN._RE_NO_SESSION),
    ("[runner] capturing with 3 parallel worker(s)...", RN._RE_WORKERS),
    ("[runner] retrying 18 link(s) sequentially...", RN._RE_RETRY),
    ("[quality] recapturing 2 low-quality screenshot(s)...", RN._RE_QUALITY),
    ("[quality] dropping 1 shot(s) still covered by an X dialog", RN._RE_BLOCKED),
    ("[quality] dropped 3 shot(s) whose parent post could not be captured",
     RN._RE_PARENT_LOST),
    ("[quality] dropped 6 shot(s) too small to contain a post", RN._RE_TOO_SMALL),
    ("[quality] 2 shot(s) may be missing the parent post or the reply",
     RN._RE_CROPPED),
    ("[verify] 20/20 links produced a clean screenshot", RN._RE_VERIFY),
    ("[metrics] 4 post(s) had at least one metric unavailable", RN._RE_METRICS),
    ("[report] wrote /app/reports/X.pdf  (1.3 MB)", RN._RE_WROTE),
    ("  [x] ok           @handle  Account Name", RN._RE_RESULT),
]
for text, regex in FROZEN_LINES:
    check(f"{text[:52]!r}", matches(regex, text))

# --------------------------------------------------------------------------- #
print("\n5. every _Progress regex is covered by at least one emitter")
covered = set()
for name, make, _r, _g in CASES:
    covered |= {rn for rn, rx in ALL_RE.items() if rx.match(make())}
covered |= {rn for rn, rx in ALL_RE.items() if rx.match(line)}
missing = set(ALL_RE) - covered
check(f"uncovered regexes: {sorted(missing)}", missing, set())

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("STDOUT CONTRACT HOLDS — the web layer will understand this runner")

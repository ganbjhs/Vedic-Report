"""Capture runner (X-only): read a list of X/Twitter post links, screenshot each
one in the logged-in browser, verify the shots, write results.json.

Capture runs in PARALLEL across worker processes, so many links are
screenshotted concurrently instead of one-by-one.

Source resolution (first match wins):
    1. a CLI argument: an .xlsx path, or "-" to paste links on stdin
    2. config/links.xlsx       (default)

Usage:
    python src/run_report.py links.xlsx
    python src/run_report.py -                      # paste links, Ctrl-D
    python src/run_report.py --workers 6            # more parallelism
    python src/run_report.py --fast                 # shorter fixed waits
    python src/run_report.py --headed               # watch the browser
    python src/run_report.py --keep-engagement      # keep the like/views line

By default the shot is cropped above the engagement bar. `--keep-engagement`
keeps it — on a comment link that means the parent's like/views line AND the
comment's own, both in frame.

The X login comes from sessions/x_state.json — create it once with:
    python src/save_sessions.py x
"""
import json
import multiprocessing
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import input_loader  # noqa: E402
import shot_quality  # noqa: E402s
import _worker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = str(Path(__file__).resolve().parent)
OUT = ROOT / "reports"
SHOTS = OUT / "screenshots"
DEFAULT_WORKERS = 3
MIN_SHOT_BYTES = 1024   # a valid PNG screenshot is comfortably larger than this

# APPROVED EDIT 6b — the recovery passes stop being single-file.
#
# Both passes were written to run in ONE browser, sequentially, and the comment
# on the retry pass says why: it recovers "transient timeouts from heavy
# parallelism", so going slower than the main pass IS the recovery. That intent
# is kept — the passes still run at a FRACTION of the main width, never at full
# width — but "one browser" and "calmer than the main pass" are not the same
# thing, and on a long list the difference is measured in half-hours: 15% of
# 1232 links is 185 posts, and 185 posts through one browser is over half an
# hour after the main pass has already finished.
#
# Below this many links a pass stays exactly as it was — single browser, in
# order. That is the case the original wording was written for and the case
# where a second browser buys nothing worth the risk.
_RECOVERY_MIN_TASKS = 40
_RECOVERY_WIDTH = 0.5          # of the main pass's worker count, rounded down


def _recovery_workers(n_tasks: int, workers: int) -> int:
    """How many browsers a recovery pass may use. 1 for a short list."""
    if n_tasks < _RECOVERY_MIN_TASKS or workers <= 1:
        return 1
    return max(1, min(int(workers * _RECOVERY_WIDTH), n_tasks))

CTX_KWARGS = {
    "viewport": {"width": 1280, "height": 1600},
    "locale": "en-IN",
    "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}


def _arg_value(argv, flag, default):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def run_tasks(tasks, workers, headless, state):
    """Capture `tasks` across `workers` browsers. APPROVED EDIT 6a.

    Was: the list was cut into `workers` fixed slices by `n % workers` up front
    and each worker process was handed one. Now: one queue, every worker pulls
    the next task, so the run ends when the WORK ends rather than when the
    unluckiest slice ends. Same tasks, same dispatch order, same per-link
    behaviour — only the assignment changes.

    MEASURED, because the obvious justification for this is wrong. "A sheet that
    groups its heavy accounts together loads them all into one slice" sounds
    right and is false: a stride-`workers` round robin spreads a contiguous
    block of expensive links *evenly*, and it spreads a periodic one evenly too.
    Simulated over 300 shuffles of a 1232-link list shaped like a real one (90%
    ordinary ~8s, 8% media-heavy ~15s, 2% dead/throttled ~60s), 4 workers:

        fixed split   mean 51.6 min      shared queue  mean 49.4 min
        wall clock removed: mean 4.2%, best 10.6%, worst 0.0%

    Four percent is not why this changed. This is why:

        one browser running 1.6x slow -> wall clock removed: mean 30.8%

    A fixed slice is decided before anything is known about how fast each
    browser will actually run, and they do not run equally: a browser that hits
    an X backoff, or that the OS deschedules because the box is oversubscribed,
    keeps its entire remaining share while the others sit finished and idle.
    On a 1-vCPU server that contention is the normal case, not the unlucky one.
    The queue cannot fix a slow browser; it stops a slow browser from deciding
    when everyone else goes home.

    One browser per worker for its whole life, as before — launching Chromium
    costs seconds, which is what makes pulling one task at a time worth doing.

    A worker that dies takes only its own in-flight task with it: it returns
    what it had captured, and the links it never reached come back through the
    retry pass (see `failed_idx` in main, which is built from the TASKS rather
    than from the results for exactly this reason).
    """
    workers = max(1, min(workers, len(tasks)))
    if workers == 1:
        return _worker.run_chunk(tasks, headless, state, CTX_KWARGS, SRC)

    manager = multiprocessing.Manager()
    queue = manager.Queue()
    for t in tasks:
        queue.put(t)
    for _ in range(workers):
        queue.put(None)                       # one sentinel per worker

    collected = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_worker.run_queue, queue, headless, state,
                             CTX_KWARGS, SRC)
                   for _ in range(workers)]
        for fut in futures:
            try:
                collected.extend(fut.result())
            except Exception as e:            # rule 17: never silent
                print(f"[runner] a capture worker died: {e} — its links will "
                      f"come back through the retry pass")
    return collected


def resolve_source(argv) -> str:
    skip = set()
    if "--workers" in argv:
        skip.add(argv.index("--workers") + 1)
    for j, a in enumerate(argv[1:], 1):
        if j in skip or a.startswith("--"):
            continue
        return a   # the first positional arg is the source (an .xlsx path or "-")
    return str(ROOT / "config" / "links.xlsx")


def x_storage_state():
    """Load only sessions/x_state.json — this project is X-only."""
    f = ROOT / "sessions" / "x_state.json"
    if not f.exists():
        print("[runner] NO saved X session — running logged-out "
              "(run: python src/save_sessions.py x)")
        return None
    try:
        d = json.loads(f.read_text())
    except Exception:
        print("[runner] x_state.json unreadable — running logged-out")
        return None
    print("[runner] loaded X session")
    return {"cookies": d.get("cookies", []), "origins": d.get("origins", [])}


def build_tasks(rows, keep_engagement: bool = False, fast: bool = False) -> list:
    tasks = []
    for i, row in enumerate(rows, 1):
        capture_url = (row.get("link") or "").strip()
        if not capture_url:
            continue
        post_link = (row.get("post_link") or "").strip() or capture_url
        account = row.get("account_name") or f"tweet_{i}"
        safe = "".join(c if c.isalnum() else "_" for c in account)[:40]
        shot = SHOTS / f"{i:02d}_{safe}.png"
        tasks.append({
            "idx": i, "capture_url": capture_url, "post_link": post_link,
            "account": account, "platform": "x",
            "category": row.get("category", "Uncategorized"),
            "shot": str(shot),
            "keep_engagement": keep_engagement,
            # Rides on the task dict for the same reason keep_engagement does:
            # `run_chunk`'s pickled signature must not move (rule 1).
            "fast": fast,
        })
    return tasks


def _shot_ok(result) -> bool:
    if result.get("status") != "ok":
        return False
    shot = result.get("screenshot")
    return bool(shot) and Path(shot).exists() and Path(shot).stat().st_size > MIN_SHOT_BYTES


def _why_poor(result):
    """Why this shot is not trustworthy, or None when it is.

    Four independent checks, strongest evidence first. The first three are facts
    the capture observed in the DOM — a dialog that was still painted over the
    post, a reply framed without the parent it demonstrably had, and a frame
    that did not reach both the parent and the reply it promised. The fourth is
    the pixel analyzer, which is the backstop for anything the DOM checks did
    not anticipate.
    """
    if result.get("overlay"):
        return "an X dialog was still covering the post"
    if result.get("parent_lost"):
        return "the reply was framed without its parent post"
    if result.get("frame_ok") is False:
        return "the frame did not cover the whole post + reply"
    good, why = shot_quality.screenshot_quality(result["screenshot"])
    return None if good else why


def _quality_ok(result) -> bool:
    """A shot that exists, isn't blank/black/half-loaded, isn't covered by a
    dialog, and actually framed what the crop promised."""
    if not _shot_ok(result):
        return False
    return _why_poor(result) is None


def _undersized(result) -> bool:
    """The frame is too small to contain a post — a measurement, not a guess.

    Separated from the other pixel checks because only this one is allowed to
    demote a link (see the final gate, and shot_quality.is_undersized).
    """
    # NOT gated on _shot_ok: that requires MIN_SHOT_BYTES, and a sliver can
    # compress below it. Such a shot would then skip this check while still
    # satisfying report_builder._usable (status "ok" + non-zero file) and land
    # in the document. Existence is the only precondition that belongs here.
    if result.get("status") != "ok":
        return False
    shot = result.get("screenshot")
    if not shot or not Path(shot).exists():
        return False
    good, why = shot_quality.screenshot_quality(shot)
    return (not good) and shot_quality.is_undersized(why)


def verify(collected) -> None:
    """Report which links produced a usable screenshot and which did not."""
    ok = [r for r in collected if _shot_ok(r)]
    bad = [r for r in collected if not _shot_ok(r)]
    print(f"[verify] {len(ok)}/{len(collected)} links produced a clean screenshot")
    for r in bad:
        why = r.get("status") if r.get("status") != "ok" else "empty/missing screenshot"
        print(f"[verify]   ✗ {r.get('account_name')}  ({why})  {r.get('post_link')}")


def main() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    argv = sys.argv
    headless = "--headed" not in argv
    workers = int(_arg_value(argv, "--workers", DEFAULT_WORKERS))
    keep_engagement = "--keep-engagement" in argv
    fast = "--fast" in argv

    rows = input_loader.load(resolve_source(argv))
    tasks = build_tasks(rows, keep_engagement, fast)
    print(f"[runner] {len(tasks)} X link(s) loaded")
    if keep_engagement:
        print("[runner] keeping the engagement line (likes / views) in frame")
    if fast:
        print("[runner] fast waits on — shorter network settle and pacing")
    if not tasks:
        print("[runner] nothing to capture"); return

    state = x_storage_state()
    workers = max(1, min(workers, len(tasks)))
    print(f"[runner] capturing with {workers} parallel worker(s)...")

    collected = run_tasks(tasks, workers, headless, state)

    # retry pass: re-attempt anything without a clean screenshot once,
    # sequentially (recovers transient timeouts from heavy parallelism).
    by_idx = {r["idx"]: r for r in collected}
    # Every task WITHOUT a clean shot — not merely every result that reported
    # one. Those differ when a worker dies mid-task: that link comes back with
    # no result at all, so a set built from `collected` would not contain it and
    # it would be dropped silently instead of retried. Same set as before for
    # every run in which nothing crashes.
    failed_idx = {t["idx"] for t in tasks if not _shot_ok(by_idx.get(t["idx"], {}))}
    if failed_idx:
        retry_tasks = [t for t in tasks if t["idx"] in failed_idx]
        rw = _recovery_workers(len(retry_tasks), workers)
        print(f"[runner] retrying {len(retry_tasks)} link(s) sequentially..."
              if rw == 1 else
              f"[runner] retrying {len(retry_tasks)} link(s) sequentially "
              f"across {rw} worker(s)...")
        recovered = 0
        for r in run_tasks(retry_tasks, rw, headless, state):
            if _shot_ok(r) or not _shot_ok(by_idx.get(r["idx"], {})):
                if _shot_ok(r) and not _shot_ok(by_idx.get(r["idx"], {})):
                    recovered += 1
                by_idx[r["idx"]] = r
        print(f"[runner] retry recovered {recovered}/{len(retry_tasks)}")

    # quality pass: recapture any 'ok' shot that came out blank / black /
    # half-loaded / still behind a sensitive gate — give it a fresh, slower try.
    poor_idx = {i for i, r in by_idx.items() if _shot_ok(r) and not _quality_ok(r)}
    if poor_idx:
        poor_tasks = [t for t in tasks if t["idx"] in poor_idx]
        print(f"[quality] recapturing {len(poor_tasks)} low-quality screenshot(s)...")
        for t in poor_tasks:
            why = _why_poor(by_idx[t["idx"]]) or "unknown"
            print(f"[quality]   ↻ {by_idx[t['idx']].get('account_name')}  ({why})")
        # recapture overwrites the same file, so always take the fresh attempt
        # and count how many now pass the quality check.
        fixed = 0
        for r in run_tasks(poor_tasks, _recovery_workers(len(poor_tasks), workers),
                           headless, state):
            by_idx[r["idx"]] = r
            if _quality_ok(r):
                fixed += 1
        print(f"[quality] improved {fixed}/{len(poor_tasks)}")

    # Final gate. A dialog that survived every retake is a FACT, not a guess —
    # the screenshot demonstrably shows a popup instead of the post, so the link
    # is reported rather than printed into the document. The pixel-based
    # judgements deliberately do NOT demote: they are heuristics, and a
    # half-dark screenshot of the right post still beats a missing page.
    blocked = [r for r in by_idx.values() if r.get("status") == "ok" and r.get("overlay")]
    for r in blocked:
        r["status"] = "overlay_blocked"
    if blocked:
        print(f"[quality] dropping {len(blocked)} shot(s) still covered by an X dialog")

    # A parent loss meets the same evidential standard: the capture SAW an
    # ancestor above this post before it scrolled, and the frame it produced
    # holds only one article. A reply printed without the post it answers is
    # wrong evidence, and wrong evidence is worse than missing evidence with an
    # explanation — so this demotes too, after every retake has been spent.
    orphaned = [r for r in by_idx.values()
                if r.get("status") == "ok" and r.get("parent_lost")]
    for r in orphaned:
        r["status"] = "parent_lost"
    if orphaned:
        print(f"[quality] dropped {len(orphaned)} shot(s) whose parent post "
              "could not be captured")
    # An undersized frame is the third observed failure, alongside a surviving
    # dialog and a lost parent. 598x80 is `_crop_box`'s floor: the crop computed
    # degenerate and the picture is a sliver of whatever happened to be on the
    # page — usually somebody else's post. Shipping that as a clean capture is
    # wrong evidence, which is worse than a missing page WITH a reason. The
    # statistical checks above still never demote.
    undersized = [r for r in by_idx.values()
                  if r.get("status") == "ok" and _undersized(r)]
    for r in undersized:
        r["status"] = "too_small"
    if undersized:
        print(f"[quality] dropped {len(undersized)} shot(s) too small to "
              "contain a post")

    cropped = [r for r in by_idx.values()
               if r.get("status") == "ok" and r.get("frame_ok") is False]
    if cropped:
        print(f"[quality] {len(cropped)} shot(s) may be missing the parent post "
              "or the reply")
        for r in cropped:
            print(f"[quality]   ! {r.get('account_name')}  {r.get('post_link')}")

    collected = list(by_idx.values())
    collected.sort(key=lambda r: r.get("idx", 0))
    for r in collected:
        r.pop("idx", None)
        print(f"  [x] {r['status']:12} {r.get('handle') or ''}  {r['account_name']}")

    verify(collected)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(collected, indent=2))
    print(f"[runner] wrote {OUT / 'results.json'}")


if __name__ == "__main__":
    main()

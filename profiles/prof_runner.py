"""Profile capture runner — the parallel of `src/run_report.py`.

Mirrors the proven shape (RULEBOOK rule 18 point 5): parallel workers, a
sequential retry pass, a quality re-capture pass, a final gate, `results.json`.
Neither `src/run_report.py` nor `influencer/inf_runner.py` is imported or
modified.

What is different, and it is the whole point: **`CTX_KWARGS` is built from the
profile**, so a profile owns its viewport and its `device_scale_factor` without
anyone touching frozen code. That is how 2x resolution ships as an opt-in
without betting the default Twitter report on it (see the dead-ends table in
RULEBOOK.md).

Every progress line goes through `progress.py`, whose output is asserted against
the web layer's real regexes by `tests/test_progress_contract.py`.

Usage (normally via run_profile.py):
    python profiles/prof_runner.py links.xlsx --profile client-deck --workers 3
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
INF = str(ROOT / "influencer")
HERE = str(Path(__file__).resolve().parent)

sys.path.insert(0, SRC)
sys.path.insert(0, HERE)
import input_loader   # noqa: E402  frozen, read-only
import shot_quality   # noqa: E402  frozen, read-only
import progress       # noqa: E402
import registry       # noqa: E402
import prof_worker    # noqa: E402

OUT = ROOT / "reports"
SHOTS = OUT / "screenshots"
MIN_SHOT_BYTES = 1024

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def ctx_kwargs_for(profile: dict) -> dict:
    """Browser context settings straight from the profile.

    `device_scale_factor` is the reason this function exists. Playwright's
    `page.screenshot()` defaults to scale="device", so raising it raises master
    resolution with no other change — every crop, clip and frame check in the
    engines works in CSS pixels and is DPR-blind (verified in the DPR benchmark).
    """
    cap = profile["capture"]
    kwargs = {
        "viewport": dict(cap["viewport"]),
        "locale": "en-IN",
        "user_agent": _UA,
    }
    dsf = cap.get("device_scale_factor") or 1
    if dsf != 1:
        kwargs["device_scale_factor"] = dsf
    return kwargs


def _arg_value(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def resolve_source(argv) -> str:
    """First positional argument, skipping every '--flag value' pair.

    Unlike the frozen `resolve_source`, this skips the value after ANY known
    value-taking flag, not just --workers. The frozen one only skips --workers,
    so `--date X links.xlsx` makes X the source path — a trap worth not
    reproducing (see docs/profile-engine.md and RULEBOOK §7 notes).
    """
    value_flags = {"--workers", "--profile", "--title", "--date"}
    skip = set()
    for i, a in enumerate(argv):
        if a in value_flags:
            skip.add(i + 1)
    for j, a in enumerate(argv[1:], 1):
        if j in skip or a.startswith("--"):
            continue
        return a
    return str(ROOT / "config" / "links.xlsx")


def x_storage_state():
    f = ROOT / "sessions" / "x_state.json"
    if not f.exists():
        progress.no_session()
        return None
    try:
        d = json.loads(f.read_text())
    except Exception:
        print("[runner] x_state.json unreadable — running logged-out", flush=True)
        return None
    print("[runner] loaded X session", flush=True)
    return {"cookies": d.get("cookies", []), "origins": d.get("origins", [])}


def build_tasks(rows) -> list:
    tasks = []
    for i, row in enumerate(rows, 1):
        capture_url = (row.get("link") or "").strip()
        if not capture_url:
            continue
        account = row.get("account_name") or f"tweet_{i}"
        safe = "".join(c if c.isalnum() else "_" for c in account)[:40]
        tasks.append({
            "idx": i, "capture_url": capture_url,
            "post_link": (row.get("post_link") or "").strip() or capture_url,
            "account": account, "platform": "x",
            "category": row.get("category", "Uncategorized"),
            "shot": str(SHOTS / f"{i:02d}_{safe}.png"),
        })
    return tasks


def _shot_ok(result) -> bool:
    if result.get("status") != "ok":
        return False
    shot = result.get("screenshot")
    return bool(shot) and Path(shot).exists() and \
        Path(shot).stat().st_size > MIN_SHOT_BYTES


def _why_poor(result):
    """Same evidence ordering as the frozen runners: DOM facts first, the pixel
    analyzer as the backstop (RULEBOOK rule 7 + rule 20)."""
    if result.get("overlay"):
        return "an X dialog was still covering the post"
    if result.get("parent_lost"):
        return "the reply was framed without its parent post"
    if result.get("frame_ok") is False:
        return "the frame did not cover the whole post + reply"
    good, why = shot_quality.screenshot_quality(result["screenshot"])
    return None if good else why


def _quality_ok(result) -> bool:
    return _shot_ok(result) and _why_poor(result) is None


def _undersized(result) -> bool:
    """Too small to contain a post — a measurement, so it may demote."""
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


def main() -> None:
    argv = sys.argv
    slug = _arg_value(argv, "--profile", "twitter")
    profile = registry.load(slug)
    engine = profile["capture"]["engine"]
    keep = bool(profile["capture"].get("keep_engagement"))

    headless = "--headed" not in argv
    default_workers = profile["capture"].get("workers") or 3
    workers = int(_arg_value(argv, "--workers", default_workers) or default_workers)
    ctx_kwargs = ctx_kwargs_for(profile)

    SHOTS.mkdir(parents=True, exist_ok=True)
    rows = input_loader.load(resolve_source(argv))
    tasks = build_tasks(rows)
    progress.total(len(tasks))
    dsf = profile["capture"].get("device_scale_factor") or 1
    print(f"[runner] profile={slug} engine={engine} dpr={dsf} "
          f"viewport={ctx_kwargs['viewport']['width']}x"
          f"{ctx_kwargs['viewport']['height']}", flush=True)
    if not tasks:
        print("[runner] nothing to capture", flush=True)
        return

    state = x_storage_state()
    workers = max(1, min(workers, len(tasks)))
    chunks = [[] for _ in range(workers)]
    for n, t in enumerate(tasks):
        chunks[n % workers].append(t)
    chunks = [c for c in chunks if c]
    progress.workers(len(chunks))

    def run(chunk_list):
        return prof_worker.run_chunk(chunk_list, headless, state, ctx_kwargs,
                                     SRC, INF, engine, keep)

    collected = []
    if len(chunks) == 1:
        collected = run(chunks[0])
    else:
        with ProcessPoolExecutor(max_workers=len(chunks)) as ex:
            futures = [ex.submit(prof_worker.run_chunk, c, headless, state,
                                 ctx_kwargs, SRC, INF, engine, keep)
                       for c in chunks]
            for fut in futures:
                collected.extend(fut.result())

    by_idx = {r["idx"]: r for r in collected}

    # retry pass — one sequential re-attempt for anything without a clean shot
    failed = {r["idx"] for r in collected if not _shot_ok(r)}
    if failed:
        retry_tasks = [t for t in tasks if t["idx"] in failed]
        progress.retrying(len(retry_tasks))
        recovered = 0
        for r in run(retry_tasks):
            was_ok = _shot_ok(by_idx.get(r["idx"], {}))
            if _shot_ok(r) and not was_ok:
                recovered += 1
            if _shot_ok(r) or not was_ok:
                by_idx[r["idx"]] = r
        progress.retry_recovered(recovered, len(retry_tasks))

    # quality pass — recapture blank / black / dialog-covered / parent-lost
    poor = {i for i, r in by_idx.items() if _shot_ok(r) and not _quality_ok(r)}
    if poor:
        poor_tasks = [t for t in tasks if t["idx"] in poor]
        progress.recapturing(len(poor_tasks))
        for t in poor_tasks:
            progress.recapture_note(by_idx[t["idx"]].get("account_name"),
                                    _why_poor(by_idx[t["idx"]]) or "unknown")
        fixed = 0
        for r in run(poor_tasks):
            by_idx[r["idx"]] = r
            if _quality_ok(r):
                fixed += 1
        progress.improved(fixed, len(poor_tasks))

    # Final gate — only OBSERVED failures demote (rule 7). A dialog that
    # survived every retake, and a reply framed without the parent it
    # demonstrably had, are both facts the capture recorded.
    blocked = [r for r in by_idx.values()
               if r.get("status") == "ok" and r.get("overlay")]
    for r in blocked:
        r["status"] = "overlay_blocked"
    if blocked:
        progress.dropping_overlay(len(blocked))

    orphaned = [r for r in by_idx.values()
                if r.get("status") == "ok" and r.get("parent_lost")]
    for r in orphaned:
        r["status"] = "parent_lost"
    if orphaned:
        progress.dropped_parent_lost(len(orphaned))

    undersized = [r for r in by_idx.values()
                  if r.get("status") == "ok" and _undersized(r)]
    for r in undersized:
        r["status"] = "too_small"
    if undersized:
        progress.dropped_too_small(len(undersized))

    cropped = [r for r in by_idx.values()
               if r.get("status") == "ok" and r.get("frame_ok") is False]
    if cropped:
        progress.maybe_cropped(len(cropped))

    results = sorted(by_idx.values(), key=lambda r: r.get("idx", 0))
    for r in results:
        r.pop("idx", None)
        progress.result_line(r["status"], r.get("handle"), r["account_name"])

    ok = [r for r in results if _shot_ok(r)]
    progress.verify(len(ok), len(results))
    for r in results:
        if not _shot_ok(r):
            progress.verify_failure(r.get("account_name"),
                                    r.get("status") or "no screenshot",
                                    r.get("post_link"))

    if engine == "influencer":
        missing = sum(1 for r in results if _shot_ok(r)
                      and any(v == "—" for k, v in (r.get("metrics") or {}).items()
                              if not k.startswith("_")))
        if missing:
            progress.metrics_missing(missing)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    progress.wrote_results(OUT / "results.json")


if __name__ == "__main__":
    main()

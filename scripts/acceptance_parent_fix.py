#!/usr/bin/env python3
"""Acceptance harness for approved edit 5 (RULEBOOK rule 20).

Runs the real pipeline in throwaway copies (RULEBOOK rule 2 isolation: the code
is copied, so each run writes into its OWN reports/) and reports, per run:

  * silent parent losses      -- MUST be 0. A reply framed alone that still
                                 shipped as status="ok" is the bug.
  * flagged parent losses     -- fine, and expected occasionally: the fix caught
                                 it, spent its retakes and demoted the link to
                                 status="parent_lost" with a stated reason.
  * session health            -- retry / recapture counts. A throttled session
                                 invalidates the numbers (rule 21), so this
                                 refuses to call a run PASS when it looks sick.

Usage
-----
    .venv/bin/python scripts/acceptance_parent_fix.py mixed   --runs 2
    .venv/bin/python scripts/acceptance_parent_fix.py roots   --runs 1
    .venv/bin/python scripts/acceptance_parent_fix.py influencer
    .venv/bin/python scripts/acceptance_parent_fix.py dpr2    --runs 2

Link sets live in scripts/acceptance/*.txt — see the README block there.
Nothing here touches the repo's own reports/ folder.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETS = Path(__file__).resolve().parent / "acceptance"
WORK = ROOT / "data" / "acceptance"          # gitignored (data/ already is)

# A run that needed this much rework is throttled, not measured (rule 21).
SICK_RETRY_SHARE = 0.20      # >20% of links needing a retry pass
SICK_TINY_SHOTS = 1          # any frame under 180px tall is a rot signal

_RE_RETRY = re.compile(r"^\[runner\]\s+retrying (\d+) link")
_RE_RECAP = re.compile(r"^\[quality\]\s+recapturing (\d+)")
_RE_TINY = re.compile(r"too-small \d+x(\d+)")
_RE_VERIFY = re.compile(r"^\[verify\]\s+(\d+)/(\d+)")


def build_copy(dest: Path, dpr: int) -> None:
    """A private copy of the pipeline. dpr=2 patches CTX_KWARGS in the COPY."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for item in ("run.py", "src", "influencer"):
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, dest / item,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dest / item)
    (dest / "sessions").symlink_to(ROOT / "sessions", target_is_directory=True)
    if dpr == 2:
        for rel in ("src/run_report.py", "influencer/inf_runner.py"):
            f = dest / rel
            s = f.read_text()
            old = 'CTX_KWARGS = {\n    "viewport"'
            new = 'CTX_KWARGS = {\n    "device_scale_factor": 2,\n    "viewport"'
            if old in s:
                f.write_text(s.replace(old, new, 1))


def analyse(app: Path, log: str) -> dict:
    """Parent losses + session health for one finished run."""
    try:
        results = json.loads((app / "reports" / "results.json").read_text())
    except (OSError, ValueError):
        return {"error": "no results.json"}

    silent, flagged = [], []
    for r in results:
        if not r.get("parent_lost"):
            continue
        (flagged if r.get("status") == "parent_lost" else silent).append(r)

    # A silent loss can also predate the fix's vocabulary: ok + frame_ok False.
    legacy_silent = [r for r in results
                     if r.get("status") == "ok" and r.get("frame_ok") is False]

    retried = sum(int(m.group(1)) for m in map(_RE_RETRY.match, log.splitlines()) if m)
    recap = sum(int(m.group(1)) for m in map(_RE_RECAP.match, log.splitlines()) if m)
    tiny = sum(1 for m in _RE_TINY.finditer(log) if int(m.group(1)) < 180)
    v = _RE_VERIFY.search(log)

    total = len(results)
    sick = (total and retried / total > SICK_RETRY_SHARE) or tiny >= SICK_TINY_SHOTS
    return {
        "total": total,
        "silent_parent_loss": len(silent),
        "legacy_silent_frame_fail": len(legacy_silent),
        "flagged_parent_loss": len(flagged),
        "retried": retried, "recaptured": recap, "tiny_frames": tiny,
        "verify": v.group(0) if v else "",
        "session_looks_throttled": bool(sick),
    }


def run_once(label: str, links: Path, entry: str, dpr: int, workers: int) -> dict:
    app = WORK / label
    build_copy(app, dpr)
    cmd = [sys.executable, "-u", entry, str(links),
           "--title", f"Acceptance {label}", "--no-date", "--workers", str(workers)]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(app), capture_output=True, text=True)
    log = proc.stdout + proc.stderr
    (WORK / f"{label}.log").write_text(log)
    out = analyse(app, log)
    out.update({"label": label, "exit_code": proc.returncode,
                "wall_seconds": round(time.time() - t0, 1),
                "app_dir": str(app)})
    return out


MODES = {
    # mode: (link set, entrypoint, dpr, workers, what it proves)
    "mixed": ("mixed20.txt", "run.py", 1, 3,
              "the fix works on a realistic mix of replies and root posts"),
    "roots": ("roots.txt", "run.py", 1, 3,
              "root-only posts still behave exactly as before the fix"),
    "influencer": ("mixed20.txt", "influencer/run_influencer.py", 1, 1,
                   "the Influencer report is unaffected end to end"),
    "dpr2": ("mixed20.txt", "run.py", 2, 3,
             "DPR 2 with the fix in place — the deferred resolution decision"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=sorted(MODES))
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    name, entry, dpr, workers, why = MODES[args.mode]
    links = SETS / name
    if not links.exists():
        print(f"missing link set: {links}\nSee {SETS/'README.md'}.")
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    print(f"== {args.mode}: {why}")
    print(f"   links={links.name} entry={entry} dpr={dpr} workers={workers}\n")

    verdicts = []
    for i in range(1, args.runs + 1):
        label = f"{args.mode}-{i}"
        print(f"-- run {i}/{args.runs} ({label}) ...", flush=True)
        res = run_once(label, links, entry, dpr, workers)
        print(json.dumps(res, indent=2))
        verdicts.append(res)
        print()

    print("=" * 62)
    ok = True
    for r in verdicts:
        silent = r.get("silent_parent_loss", -1) + r.get("legacy_silent_frame_fail", 0)
        throttled = r.get("session_looks_throttled")
        if r.get("error") or r.get("exit_code") != 0:
            state, ok = "ERROR", False
        elif throttled:
            state = "INCONCLUSIVE (session throttled — rest and re-run, rule 21)"
            ok = False
        elif silent == 0:
            state = "PASS"
        else:
            state, ok = "FAIL", False
        print(f"  {r['label']:14} {state}   silent={silent} "
              f"flagged={r.get('flagged_parent_loss')} "
              f"retried={r.get('retried')} tiny={r.get('tiny_frames')}")
    print("=" * 62)
    print("\nNow OPEN THE DOCUMENTS AND LOOK AT THEM (rule 3):")
    for r in verdicts:
        print(f"  open {r.get('app_dir')}/reports/")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The dispatch table must not change what the existing report types do.

THE RISK. `build_command` used to decide everything with
`influencer = report_type != "twitter"`. Replacing that with a table is exactly
the kind of refactor that quietly alters a command line — and the command line
IS the working tool. So this asserts the produced argv against the literal
strings the old code built, for every combination of flags.

It also pins the invariant that made the old binary dangerous: an unknown slug
used to run the influencer report silently. It must now raise.

Zero captures.

    .venv/bin/python profiles/tests/test_dispatch.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from webapp import config, report_types as RT       # noqa: E402
from webapp.jobs import runner as RN                # noqa: E402

FAILS = []
PY = sys.executable


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got}\n        want {want}")
        FAILS.append(name)


# --------------------------------------------------------------------------- #
print("\n1. twitter: argv identical to the pre-refactor command")
check("defaults",
      RN.build_command("twitter", "My Report", "04-08-26"),
      [PY, "-u", "run.py", "input.xlsx", "--title", "My Report",
       "--date", "04-08-26", "--no-date",
       "--workers", str(config.CAPTURE_WORKERS)])

check("keep_engagement adds the switch",
      RN.build_command("twitter", "T", "04-08-26", keep_engagement=True),
      [PY, "-u", "run.py", "input.xlsx", "--title", "T", "--date", "04-08-26",
       "--no-date", "--workers", str(config.CAPTURE_WORKERS),
       "--keep-engagement"])

check("explicit workers is honoured",
      RN.build_command("twitter", "T", "04-08-26", workers=2)[-1], "2")

check("workers clamped to MAX_WORKERS",
      RN.build_command("twitter", "T", "04-08-26", workers=999)[-1],
      str(config.MAX_WORKERS))

# --------------------------------------------------------------------------- #
print("\n2. influencer: argv identical, and its invariants hold")
check("defaults",
      RN.build_command("influencer", "Inf", "04-08-26"),
      [PY, "-u", "influencer/run_influencer.py", "input.xlsx",
       "--title", "Inf", "--date", "04-08-26", "--no-date",
       "--workers", str(config.INFLUENCER_WORKERS)])

check("worker picker CANNOT override INFLUENCER_WORKERS (rule 12)",
      RN.build_command("influencer", "Inf", "04-08-26", workers=8)[-1],
      str(config.INFLUENCER_WORKERS))

check("keep_engagement is never passed to it",
      "--keep-engagement" in RN.build_command(
          "influencer", "Inf", "04-08-26", keep_engagement=True), False)

# --------------------------------------------------------------------------- #
print("\n3. an unknown slug RAISES instead of silently running influencer")
for bad in ("nope", "", "Twitter", "../run.py", "influencer2"):
    try:
        cmd = RN.build_command(bad, "T", "04-08-26")
        check(f"{bad!r} rejected", f"returned {cmd[2]}", "raised JobFailed")
    except RN.JobFailed:
        print(f"  PASS  {bad!r} rejected")
    except Exception as e:
        check(f"{bad!r} rejected", f"{type(e).__name__}", "JobFailed")

# --------------------------------------------------------------------------- #
print("\n4. profile-backed types route to run_profile.py")
for slug in ("contact-sheet", "client-deck"):
    cmd = RN.build_command(slug, "P", "04-08-26")
    check(f"{slug}: entrypoint + --profile",
          cmd[2:6], ["profiles/run_profile.py", "--profile", slug, "input.xlsx"])
    check(f"{slug}: never gets --keep-engagement",
          "--keep-engagement" in RN.build_command(
              slug, "P", "04-08-26", keep_engagement=True), False)

# --------------------------------------------------------------------------- #
print("\n5. capability flags encode the reasons, not the slug")
tw, inf = RT.get("twitter"), RT.get("influencer")
check("twitter allows both", (tw.allows_worker_choice,
                              tw.allows_keep_engagement), (True, True))
check("influencer allows neither", (inf.allows_worker_choice,
                                    inf.allows_keep_engagement), (False, False))
check("influencer default workers = INFLUENCER_WORKERS",
      inf.default_workers(), config.INFLUENCER_WORKERS)
check("twitter default workers = CAPTURE_WORKERS",
      tw.default_workers(), config.CAPTURE_WORKERS)
check("every profile type declares outputs",
      all(rt.outputs for rt in RT.all_types()), True)
check("get() on an unknown slug returns None", RT.get("nope"), None)

# --------------------------------------------------------------------------- #
print("\n6. the job dir copies the profiles package")
check("profiles is in _CODE_ITEMS", "profiles" in RN._CODE_ITEMS, True)
for item in RN._CODE_ITEMS:
    check(f"{item} exists in the repo", (ROOT / item).exists(), True)

print("\n7. html is a downloadable kind and gets published")
from webapp import routes_jobs as RJ                # noqa: E402
check("html in _KINDS", "html" in RJ._KINDS, True)
check("publish() looks for html",
      "html" in RN.publish.__code__.co_consts or
      any("html" in str(c) for c in RN.publish.__code__.co_consts), True)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("DISPATCH OK — existing report types produce identical commands")

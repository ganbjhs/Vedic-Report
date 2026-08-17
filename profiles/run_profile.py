#!/usr/bin/env python3
"""One command: capture every X link under a profile, then build its documents.

    python profiles/run_profile.py links.xlsx --profile client-deck
    python profiles/run_profile.py links.xlsx --profile contact-sheet --workers 4
    python profiles/run_profile.py links.xlsx --profile combined-16x9 --outputs pdf
    python profiles/run_profile.py - --profile client-deck        # paste on stdin

Deliberately mirrors the CLI surface of the frozen `run.py` — same `--title`,
`--date`, `--no-date`, `--workers`, `--headed` with the same meanings, the same
`"<title> <date>"` header rule and the same `"<Title>_<date>"` output stem — so
`webapp/jobs/runner.py::build_command` drives every report type through one
shape. `--profile <slug>` is the only addition, plus `--outputs pdf,pptx` — a
FILTER over the documents the style already declares, never a way to ask for
one it does not build.

`run.py` and everything under `src/` are untouched.
"""
import datetime
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import prof_runner        # noqa: E402
import prof_builder       # noqa: E402
import registry           # noqa: E402


def _take_flag(argv, flag):
    """Pop '--flag value' out of argv, returning the value (or None).

    Note the frozen `_take_flag` deletes argv[i:i+2] even when the value is
    missing; this one does not, so a trailing '--title' cannot eat the next
    unrelated argument.
    """
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 >= len(argv):
        del argv[i:i + 1]
        return None
    val = argv[i + 1]
    del argv[i:i + 2]
    return val


def _take_switch(argv, flag):
    if flag in argv:
        argv.remove(flag)
        return True
    return False


def main():
    argv = sys.argv[:]
    bare = _take_switch(argv, "--no-date")
    slug = _take_flag(argv, "--profile") or "twitter"
    outputs = _take_flag(argv, "--outputs") or ""

    # Validate BEFORE a browser starts. A bad profile should cost nothing —
    # and it must fail with a readable line, not a traceback, because the web
    # layer surfaces the subprocess's last stdout lines to the user.
    try:
        profile = registry.load(slug)
    except registry.ProfileError as e:
        print(f"[profile] {e}", flush=True)
        print(f"[profile] available: {', '.join(registry.available())}",
              flush=True)
        return 2

    title = _take_flag(argv, "--title") or profile["label"]
    date = _take_flag(argv, "--date") or datetime.date.today().strftime("%d-%m-%y")
    header = title if bare else f"{title} {date}"
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_",
                  title if bare else f"{title}_{date}").strip("_")

    # 1) capture + verify — prof_runner reads sys.argv (source / --workers /
    #    --headed / --profile), so --profile goes back on for it.
    sys.argv = argv + ["--profile", slug]
    prof_runner.main()

    # 2) build the documents this profile asks for
    sys.argv = ["prof_builder", header, stem, slug, outputs]
    prof_builder.main()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

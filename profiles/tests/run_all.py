#!/usr/bin/env python3
"""Run every zero-capture profile-engine test.

    .venv/bin/python profiles/tests/run_all.py

No browser, no network, no X account — so this is safe to run as often as you
like, which is the point (RULEBOOK rule 21: captures are the scarce resource).
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ("test_shapes.py", "test_parity.py",
          "test_progress_contract.py", "test_builder.py",
          "test_dispatch.py", "test_inputs.py",
          "test_sheets.py", "test_sectioned_sheet.py",
          "test_projects.py", "test_smartsheet.py")


def main() -> int:
    failed = []
    for name in SUITES:
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
        rc = subprocess.run([sys.executable, str(HERE / name)]).returncode
        if rc != 0:
            failed.append(name)
    print(f"\n{'=' * 70}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"ALL {len(SUITES)} SUITES PASSED — zero captures used")
    return 0


if __name__ == "__main__":
    sys.exit(main())

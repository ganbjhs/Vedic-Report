#!/usr/bin/env python3
"""Input parsing: messy paste, row-numbered rejections, duplicate detection.

This is the product logic behind /api/preview — "paste anything, see exactly
what will be captured before spending a capture slot". Zero captures.

    .venv/bin/python profiles/tests/test_inputs.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from webapp import config, uploads          # noqa: E402
from webapp import routes_jobs as RJ        # noqa: E402

FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          ("" if ok else f"\n        got  {got!r}\n        want {want!r}"))
    if not ok:
        FAILS.append(name)


def links(grid):
    return [c for row in grid for c in row]


# --------------------------------------------------------------------------- #
print("\n1. messy paste: URLs are extracted, junk is ignored")

MESSY = """
Hey team, here are the ones from this morning —

  1. https://x.com/alice/status/111  (this one did really well)
  2) look at https://x.com/bob/status/222, and https://x.com/carol/status/333 too

random note to self: ask design about the banner

<https://x.com/dave/status/444>
   https://twitter.com/erin/status/555
https://x.com/frank/status/666.
"""
grid = uploads.grid_from_text(MESSY)
got = links(grid)
check("found all 6 links", len(got), 6)
check("two links on one line both captured",
      "https://x.com/bob/status/222" in got and
      "https://x.com/carol/status/333" in got)
check("trailing comma stripped", "https://x.com/bob/status/222" in got)
check("trailing period stripped", "https://x.com/frank/status/666" in got)
check("angle brackets stripped", "https://x.com/dave/status/444" in got)
check("prose lines produced no rows", all("status/" in u for u in got))
check("twitter.com kept", "https://twitter.com/erin/status/555" in got)

print("\n   ...and the frozen loader reads that shape correctly")
report = uploads.analyse(grid)
check("6 rows survive the frozen loader", len(report["rows"]), 6)
check("link is the URL alone, not the whole line",
      report["rows"][0]["link"], "https://x.com/alice/status/111")
check("account derived from the handle",
      report["rows"][0]["account_name"], "@alice")

# --------------------------------------------------------------------------- #
print("\n2. non-X links are rejected WITH THEIR ROW NUMBER (roadmap A5)")
MIXED = "\n".join([
    "https://x.com/a/status/1",
    "https://facebook.com/somepost",
    "https://x.com/b/status/2",
    "https://youtube.com/watch?v=abc",
    "https://x.com/c/status/3",
])
grid = [[line] for line in MIXED.splitlines()]
report = uploads.analyse(grid)
check("3 X links kept", len(report["rows"]), 3)
check("2 rejected", len(report["dropped"]), 2)
rows_flagged = sorted(d["row"] for d in report["dropped"])
check("row numbers point at the right lines", rows_flagged, [2, 4])
check("reason names the problem",
      all("not an x.com" in d["reason"] for d in report["dropped"]))
check("the offending value is echoed back",
      report["dropped"][0]["value"], "https://facebook.com/somepost")

# --------------------------------------------------------------------------- #
print("\n3. duplicates are detected across tracking-param variants")
DUPES = "\n".join([
    "https://x.com/a/status/100",
    "https://x.com/a/status/100?s=20&t=abcdef",
    "https://x.com/b/status/200",
    "https://x.com/a/status/100",
    "https://x.com/c/status/300",
])
grid = [[line] for line in DUPES.splitlines()]
report = uploads.analyse(grid)
check("all 5 kept when not deduping (frozen behaviour unchanged)",
      len(report["rows"]), 5)
check("one duplicate group found", len(report["duplicates"]), 1)
check("3 occurrences of the same post",
      report["duplicates"][0]["positions"], [1, 2, 4])
check("duplicate_count counts the extras", report["duplicate_count"], 2)

deduped = uploads.analyse(grid, dedupe=True)
check("dedupe keeps the first of each", len(deduped["rows"]), 3)
check("order preserved",
      [r["link"] for r in deduped["rows"]],
      ["https://x.com/a/status/100", "https://x.com/b/status/200",
       "https://x.com/c/status/300"])

# --------------------------------------------------------------------------- #
print("\n4. a spreadsheet grid still behaves exactly as before")
SHEET = [["Account", "Link"],
         ["Alice", "https://x.com/alice/status/1"],
         ["Bob", "https://x.com/bob/status/2"],
         ["Spam", "https://linkedin.com/in/nope"]]
report = uploads.analyse(SHEET)
check("header row honoured, 2 links kept", len(report["rows"]), 2)
check("account column used", report["rows"][0]["account_name"], "Alice")
check("non-X row rejected with its row number",
      [(d["row"], "linkedin" in d["value"]) for d in report["dropped"]],
      [(4, True)])

# --------------------------------------------------------------------------- #
print("\n5. limits and empties")
big = [[f"https://x.com/u/status/{i}"] for i in range(config.MAX_LINKS + 5)]
report = uploads.analyse(big)
check("over_limit flagged", report["over_limit"], True)
check("limit reported", report["limit"], config.MAX_LINKS)
check("empty text gives no rows", uploads.grid_from_text(""), [])
check("prose with no links gives no rows",
      uploads.grid_from_text("nothing to see here\njust words"), [])
check("empty grid analyses cleanly", uploads.analyse([])["rows"], [])

# --------------------------------------------------------------------------- #
print("\n6. preview and submit share one parsing path")
import inspect                                    # noqa: E402
src_preview = inspect.getsource(RJ.preview)
src_submit = inspect.getsource(RJ.submit_job)
check("preview calls _grid_from_request", "_grid_from_request" in src_preview)
check("submit calls _grid_from_request", "_grid_from_request" in src_submit)
check("preview calls uploads.analyse", "uploads.analyse" in src_preview)
check("submit calls uploads.analyse", "uploads.analyse" in src_submit)

print("\n7. the routes exist with the right methods")
by_path = {r.path: sorted(getattr(r, "methods", []) or [])
           for r in RJ.router.routes}
check("/api/preview is POST", by_path.get("/api/preview"), ["POST"])
check("/api/jobs is POST", by_path.get("/api/jobs"), ["POST"])
check("preview accepts file, text, dedupe, csrf",
      {"file", "text", "dedupe", "csrf_token"}
      <= set(inspect.signature(RJ.preview).parameters), True)
check("submit accepts the same input methods",
      {"file", "text", "dedupe"}
      <= set(inspect.signature(RJ.submit_job).parameters), True)
check("file is OPTIONAL on submit now (paste needs no file)",
      inspect.signature(RJ.submit_job).parameters["file"].default is not ...,
      True)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("INPUT PARSING OK — paste, sheet and file agree on what a link is")

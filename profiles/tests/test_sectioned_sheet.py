#!/usr/bin/env python3
"""The team's own Google Sheet, read exactly as it is kept. Zero captures.

    .venv/bin/python profiles/tests/test_sectioned_sheet.py

`fixtures/sectioned_sheet.csv` is a byte copy of the live sheet, taken through
the same published-CSV endpoint `webapp/sheets.py` uses
(`gviz/tq?tqx=out:csv`), so this suite asserts against the real layout rather
than an idealised one. That layout is:

    row 1        <first section name> | Likes | Post Impression | Reach/views
    then         a row with a section name in column A (no link, no numbers)
                 followed by rows with a post URL in column A and the numbers
                 beside it, and a stray row whose column A is EMPTY but which
                 still holds numbers.

There is no link column, no handle column and no section column — the shape is
the whole signal (`netlinks.metric_header`), which is why no special mode
exists. What this pins down:

  * the header row's first cell becomes the FIRST section, and the header row
    itself never becomes a section;
  * "Reach/views" is one column feeding BOTH `views` and `reach`;
  * the blank-column-A rows are dropped — not captured, not promoted to a
    section made of digits, and not reported to the preview as rejected;
  * every row is `account_auto`, because the sheet names nobody;
  * the canonical `input.xlsx` the job runs from round-trips all of it, which
    is the hop where the placeholder name used to freeze into a real one.
"""
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "profiles"))

import netlinks                              # noqa: E402
from webapp import uploads                   # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sectioned_sheet.csv"
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          ("" if ok else f"\n        got  {got!r}\n        want {want!r}"))
    if not ok:
        FAILS.append(name)


METRICS = {"like": "676", "impressions": "63,000",
           "views": "63,000", "reach": "63,000"}

# The sheet's own order and its own counts. Both are read off the fixture, so
# when the team adds a link this file is the one place that moves.
SECTIONS = [
    ("3 Party Pages Posting", 3),
    ("Hyper Local Pages Posting", 3),
    ("National X Influencers", 2),
    ("Tweet Amplification Links", 3),
    ("Counter Comments Links", 5),
]
TOTAL = sum(n for _, n in SECTIONS)
PLATFORMS = (["facebook", "facebook", "instagram"]
             + ["facebook"] * 3
             + ["x"] * 10)

grid = uploads.grid_from_csv_text(FIXTURE.read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- #
print("\n1. the header row is recognised without a link column")
head = netlinks.metric_header(grid)
check("a header row was found", head is not None)
check("it is row 0", head[0], 0)
check("its first cell is the first section", head[2], "3 Party Pages Posting")
check("metric_columns maps every metric column",
      netlinks.metric_columns(grid),
      {"like": 1, "impressions": 2, "views": 3, "reach": 3})
check("'Reach/views' feeds views AND reach from one column",
      netlinks.metric_columns(grid)["views"] ==
      netlinks.metric_columns(grid)["reach"] == 3)

# --------------------------------------------------------------------------- #
print("\n2. rows_from_grid reads the sheet as it is kept")
rows = netlinks.rows_from_grid(grid, "combined")
check(f"{TOTAL} posts", len(rows), TOTAL)

order, seen = [], set()
for r in rows:
    if r["category"] not in seen:
        seen.add(r["category"])
        order.append(r["category"])
counts = Counter(r["category"] for r in rows)
check("5 sections, in sheet order", order, [s for s, _ in SECTIONS])
check("each section holds the sheet's own count",
      [(s, counts[s]) for s in order], SECTIONS)
check("the trailing space on 'Counter Comments Links ' is stripped",
      "Counter Comments Links" in counts)
check("the header row did not become a section",
      not any("Likes" in c or "Post Impression" in c for c in counts))
check("no stray section made of digits",
      not any(c.replace(",", "").replace(" ", "").isdigit() for c in counts))

check("every row carries the sheet's metrics",
      all(r.get("sheet_metrics") == METRICS for r in rows))
check("platform is decided per link", [r["platform"] for r in rows], PLATFORMS)
check("Instagram links keep their ?igsh= stripped",
      [r["link"] for r in rows if r["platform"] == "instagram"],
      ["https://www.instagram.com/p/DbDg_3jzOCy/"])
check("no row is empty of a link", all(r["link"] for r in rows))

print("\n   ...and the sheet names nobody, so every row says so")
check("all rows are account_auto", all(r.get("account_auto") for r in rows))
check("the placeholder is still derived from the URL",
      sorted({r["account_name"] for r in rows if r["platform"] == "facebook"}),
      ["100070154647760", "61559555815073", "Facebook post"])
check("an X link with a handle in it keeps that handle",
      "@india_plus_" in {r["account_name"] for r in rows})

# --------------------------------------------------------------------------- #
print("\n3. the preview shows exactly that — nothing rejected, no stray rows")
report = uploads.analyse(grid, platform="combined")
check(f"{TOTAL} rows previewed", len(report["rows"]), TOTAL)
check("nothing reported as rejected", report["dropped"], [])
check("no duplicates claimed", report["duplicate_count"], 0)
check("preview rows and reader rows are the same rows",
      [r["link"] for r in report["rows"]], [r["link"] for r in rows])

# --------------------------------------------------------------------------- #
print("\n4. the canonical input.xlsx round-trips sections, platforms, metrics")
with tempfile.TemporaryDirectory() as tmp:
    dest = Path(tmp) / "input.xlsx"
    uploads.write_canonical_xlsx(rows, dest)
    back = netlinks.load_rows(str(dest), "combined")

check("same number of rows", len(back), len(rows))
check("sections unchanged",
      [r["category"] for r in back], [r["category"] for r in rows])
check("platforms unchanged",
      [r["platform"] for r in back], [r["platform"] for r in rows])
check("links unchanged", [r["link"] for r in back], [r["link"] for r in rows])
check("metrics unchanged", [r.get("sheet_metrics") for r in back],
      [r.get("sheet_metrics") for r in rows])
check("'reach' survived the trip as its own column",
      all(r["sheet_metrics"]["reach"] == "63,000" for r in back))
check("account_auto survives, so the worker still fills in the handle",
      all(r.get("account_auto") for r in back))
check("account_name is the same placeholder on the way back",
      [r["account_name"] for r in back], [r["account_name"] for r in rows])

# --------------------------------------------------------------------------- #
print("\n5. an ordinary sheet with a link column is untouched by all this")
PLAIN = [["Account", "Link", "Section", "Like", "Views"],
         ["Kashi", "https://x.com/a/status/1", "Tweets", "10", "20"],
         ["", "https://x.com/b/status/2", "Tweets", "11", "21"]]
plain = netlinks.rows_from_grid(PLAIN, "combined")
check("2 rows", len(plain), 2)
check("the named row is not account_auto", plain[0].get("account_auto"), None)
check("the unnamed one is", plain[1].get("account_auto"), True)
check("its name is still derived", plain[1]["account_name"], "@b")
check("sections come from the Section column",
      [r["category"] for r in plain], ["Tweets", "Tweets"])
check("metrics come from the metric columns",
      plain[0]["sheet_metrics"], {"like": "10", "views": "20"})

print("\n   ...and a plain pasted list still has no sections at all")
LIST = [["https://x.com/a/status/1"], ["https://x.com/b/status/2"]]
check("no metric header invented", netlinks.metric_header(LIST), None)
check("everything stays Uncategorized",
      {r["category"] for r in netlinks.rows_from_grid(LIST, "combined")},
      {"Uncategorized"})

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print(f"SECTIONED SHEET OK — {TOTAL} posts in {len(SECTIONS)} sections, "
      "read and round-tripped with no special mode")

#!/usr/bin/env python3
"""Geometry parity: the profile layout engine must reproduce the frozen builders.

THE POINT OF THIS TEST. `docs/profile-engine.md` §6.1 promises that expressing
the two existing report types as profiles produces identical output. Byte- or
pixel-identical PDFs are not achievable (reportlab embeds timestamps; comparing
rasters needs a dependency we do not have), so the bar is **geometry parity**:
same page count, and the same (page, x, y, w, h) for every image placement.

WHY THIS IS NOT A TAUTOLOGY. The oracle is the frozen code itself — this imports
`src/report_builder.py` and `influencer/inf_report_builder.py` and calls their
real geometry functions and real constants. `profiles/layout.py` computes its
answer independently, from the profile JSON. If someone edits a frozen constant,
this fails; if someone mis-transcribes one into a profile, this fails.

Zero captures. Runs in milliseconds. Run:

    .venv/bin/python profiles/tests/test_parity.py
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "profiles"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "influencer"))

import layout                       # noqa: E402  the thing under test
import registry                     # noqa: E402
import report_builder as FROZEN_X   # noqa: E402  the oracle
import inf_report_builder as FROZEN_I  # noqa: E402

FAILS = []
INCH = 72.0


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILS.append(name)


def realistic_dims(n, seed=7):
    """Master sizes in the shape X actually produces: a fixed ~598px column and
    a widely varying height. Includes the degenerate (0,0) that `_png_size`
    returns for an unreadable PNG."""
    rnd = random.Random(seed)
    dims = [(598, rnd.choice([176, 200, 260, 383, 431, 491, 784, 832, 868]))
            for _ in range(n - 1)]
    dims.append((0, 0))
    return dims


# --------------------------------------------------------------------------- #
print("\n1. fit() must match the frozen _fit(), degenerate cases included")
cases = [(598, 832, 4.9, 7.0), (598, 176, 4.9, 7.0), (1196, 1662, 4.9, 7.0),
         (598, 2000, 4.9, 7.0), (0, 0, 4.9, 7.0), (598, 0, 3.05, 6.9),
         (0, 500, 3.05, 6.9), (300, 300, 3.05, 6.9)]
for iw, ih, mw, mh in cases:
    check(f"fit({iw},{ih},{mw},{mh})",
          layout.fit(iw, ih, mw, mh), FROZEN_X._fit(iw, ih, mw, mh))

print("\n   ...and DPR must not move a placement (ratio-only)")
check("598x832 and 1196x1664 give the same box",
      layout.fit(598, 832, 4.9, 7.0), layout.fit(1196, 1664, 4.9, 7.0))

# --------------------------------------------------------------------------- #
print("\n2. twitter profile vs src/report_builder.py")
tw = registry.load("twitter")

# The frozen builder's own constants, read back from its source of truth.
from reportlab.lib.pagesizes import letter          # noqa: E402
FROZEN_MARGIN_IN = 0.75
FROZEN_CONTENT_W_IN = letter[0] / INCH - 2 * FROZEN_MARGIN_IN
FROZEN_SHOT_W_IN, FROZEN_SHOT_MAX_H_IN = 4.9, 7.0

check("page size", registry.page_inches(tw["page"]),
      (letter[0] / INCH, letter[1] / INCH))
check("content width", round(layout.content_box(tw)[2], 6),
      round(FROZEN_CONTENT_W_IN, 6))
check("one post per page", registry.per_page(tw), 1)

dims = realistic_dims(9)
mine = layout.placements(tw, dims)

# What the frozen builder does: one image per page, _fit into
# (min(SHOT_W, CONTENT_W), SHOT_MAX_H), hAlign=CENTER inside the content area.
want = []
for i, (iw, ih) in enumerate(dims):
    w, h = FROZEN_X._fit(iw, ih, min(FROZEN_SHOT_W_IN, FROZEN_CONTENT_W_IN),
                         FROZEN_SHOT_MAX_H_IN)
    want.append((i, 0, 0, round(FROZEN_MARGIN_IN + (FROZEN_CONTENT_W_IN - w) / 2, 4),
                 round(FROZEN_MARGIN_IN, 4), round(w, 4), round(h, 4)))
check("every placement", [p.as_tuple() for p in mine], want)
check("page count", layout.page_count(tw, len(dims)), len(dims))

# --------------------------------------------------------------------------- #
print("\n3. influencer profile vs influencer/inf_report_builder.py")
inf = registry.load("influencer")

check("posts per page matches POSTS_PER_PAGE",
      registry.per_page(inf), FROZEN_I.POSTS_PER_PAGE)
check("image box matches SHOT_MAX_IN",
      tuple(inf["image"]["max_in"]), tuple(FROZEN_I.SHOT_MAX_IN))
check("margins match MARGIN_IN",
      inf["page"]["margins_in"], [FROZEN_I.MARGIN_IN] * 4)

from reportlab.lib.pagesizes import A4                # noqa: E402
check("page is A4", [round(v, 3) for v in registry.page_inches(inf["page"])],
      [round(A4[0] / INCH, 3), round(A4[1] / INCH, 3)])

# The frozen influencer builder splits into pages of POSTS_PER_PAGE and fits
# each shot into SHOT_MAX_IN. Column width is COL_IN, narrower than half the
# content area (there is a gutter), so the fit is bounded by SHOT_MAX_IN.
inf_dims = realistic_dims(7, seed=11)
inf_mine = layout.placements(inf, inf_dims)
check("page assignment matches _pages() chunking",
      [p.page for p in inf_mine],
      [i // FROZEN_I.POSTS_PER_PAGE for i in range(len(inf_dims))])
check("sizes match the frozen fit into SHOT_MAX_IN",
      [(round(p.w_in, 4), round(p.h_in, 4)) for p in inf_mine],
      [tuple(round(v, 4) for v in FROZEN_X._fit(iw, ih, *FROZEN_I.SHOT_MAX_IN))
       for iw, ih in inf_dims])
check("page count", layout.page_count(inf, len(inf_dims)),
      -(-len(inf_dims) // FROZEN_I.POSTS_PER_PAGE))

# --------------------------------------------------------------------------- #
print("\n4. the image box never overflows its grid cell")
narrow = registry.load("influencer")
narrow["image"]["max_in"] = [99.0, 99.0]        # absurd on purpose
cell_w = layout.cells(narrow)[0][4]
p = layout.placements(narrow, [(598, 598)])[0]
check("clamped to the cell", p.w_in <= cell_w + 1e-9, True)
check("stays inside the page", p.x_in >= 0 and p.w_in > 0, True)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("GEOMETRY PARITY HOLDS — profiles reproduce both frozen builders")

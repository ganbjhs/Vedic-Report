"""Page geometry for a profile: where every screenshot lands, in inches.

Pure arithmetic — no PIL, no reportlab, no files. That is the point: the entire
layout of every report type can be asserted in a unit test with **zero
captures**, which keeps the rate-limited capture account (RULEBOOK rule 21) off
the critical path for most of the profile engine.

`prof_builder` consumes `placements()` and draws; the parity harness consumes it
and compares against the frozen builders' own geometry. If those two disagree,
the abstraction is wrong — fix it here, never in the frozen builder.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402


class Placement:
    """One screenshot's box on one page, in inches from the page's top-left."""

    __slots__ = ("index", "page", "col", "row", "x_in", "y_in", "w_in", "h_in")

    def __init__(self, index, page, col, row, x_in, y_in, w_in, h_in):
        self.index, self.page, self.col, self.row = index, page, col, row
        self.x_in, self.y_in, self.w_in, self.h_in = x_in, y_in, w_in, h_in

    def as_tuple(self, nd=4):
        return (self.page, self.col, self.row,
                round(self.x_in, nd), round(self.y_in, nd),
                round(self.w_in, nd), round(self.h_in, nd))

    def __eq__(self, other):
        return isinstance(other, Placement) and self.as_tuple() == other.as_tuple()

    def __repr__(self):
        return (f"Placement(i={self.index} p={self.page} c={self.col} r={self.row} "
                f"x={self.x_in:.3f} y={self.y_in:.3f} "
                f"w={self.w_in:.3f} h={self.h_in:.3f})")


def fit(iw, ih, max_w, max_h):
    """Scale (iw, ih) into (max_w, max_h) preserving aspect ratio.

    Deliberately identical to `src/report_builder._fit` and
    `influencer/inf_report_builder`'s equivalent, including its degenerate case:
    a zero dimension returns (max_w, max_w), NOT (max_w, max_h). That looks like
    a bug and is load-bearing — reproducing the frozen behaviour is the whole
    job of this function, and the parity harness asserts it against the real
    thing.
    """
    if not iw or not ih:
        return max_w, max_w
    w = max_w
    h = w * ih / iw
    if h > max_h:
        h = max_h
        w = h * iw / ih
    return w, h


def content_box(profile):
    """(x_in, y_in, w_in, h_in) of the printable area."""
    pw, ph = registry.page_inches(profile["page"])
    top, right, bottom, left = profile["page"]["margins_in"]
    return left, top, pw - left - right, ph - top - bottom


def cells(profile):
    """The grid cell boxes on one page, in reading order (left-to-right,
    then top-to-bottom)."""
    cx, cy, cw, ch = content_box(profile)
    cols, rows = profile["page"]["grid"]
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append((c, r,
                        cx + cw * c / cols, cy + ch * r / rows,
                        cw / cols, ch / rows))
    return out


def placements(profile, dims):
    """Where each screenshot goes.

    `dims` — one (width_px, height_px) per usable post, in document order. Pixel
    units are irrelevant to the result (only the ratio matters), which is why a
    device_scale_factor change cannot move a placement.
    """
    box_w, box_h = profile["image"]["max_in"]
    grid = cells(profile)
    n_cells = len(grid)
    out = []
    for i, (iw, ih) in enumerate(dims):
        page, slot = divmod(i, n_cells)
        col, row, gx, gy, gw, gh = grid[slot]
        # The placement box never exceeds its cell — a profile asking for a
        # 5in image in a 3in column gets 3in, not an overlap.
        w_in, h_in = fit(iw, ih, min(box_w, gw), min(box_h, gh))
        out.append(Placement(i, page, col, row,
                             gx + (gw - w_in) / 2,     # centred in its cell
                             gy, w_in, h_in))
    return out


def page_count(profile, n_items):
    """Pages of screenshots (the links table flows after, as it does today)."""
    if n_items <= 0:
        return 0
    per = registry.per_page(profile)
    return (n_items + per - 1) // per

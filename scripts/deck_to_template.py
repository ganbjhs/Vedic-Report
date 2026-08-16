"""Turn the Kashi 10-08-2026 Canva deck into template ART for combined-16x9.

The deck's pages carry both the design AND one report's data. A template page
image must carry the design ONLY — the app prints the handle, the date, the
metric values, the post number and the platform logo into its slots, and paints
the screenshot into the image slot. So every printed value is cleared here, and
nothing else is touched.

Three regions need more than a flat fill:

  * the value pills — cleared by redrawing the pill's own stadium shape in its
    own colour, so the rounded ends survive a value whose digits reach them;
  * the LINK chip — only the word is cleared, inside the white inner plate; the
    chain icon and the orange pill stay, and the app prints LINK (which is what
    carries the hyperlink) back into the same place;
  * the platform-logo circle sits partly over the ghat photo, and no page of
    the deck shows what is behind it. The photo's edge is a straight diagonal
    there (fitted on the rows just above the circle), so the small triangle of
    photo the circle hides is cloned from the photo immediately to its left and
    everything right of the edge becomes white. The app's logo covers most of
    it again.

CALIBRATED TO THE 10 AUGUST 2026 DECK. Every rectangle below is a measured
position in that deck's own points (its page is 960x540 pt, which is exactly
what the profile renders at, so deck points ARE profile points). If the design
moves, re-measure before re-running — do not assume these still fit:

    pdftotext -bbox -f 3 -l 3 deck.pdf p3.html    # text baselines and boxes
    pdftocairo -svg -f 3 -l 3 deck.pdf p3.svg     # exact colours per text run

and open the result (RULEBOOK rule 3) — a mask that is 2pt out eats a line of
the art, and the page still looks plausible in a thumbnail.

Run: python scripts/deck_to_template.py <deck.pdf> <outdir>
then copy post/cover/summary.png into profiles/registry/assets/combined-16x9/.
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PDF = sys.argv[1]
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
W, H = 1920, 1080          # 16:9 at 144 dpi — 960 x 540 pt, the deck's own size
WHITE = (255, 255, 255)


def render(page: int) -> Image.Image:
    dst = OUT / f"_raw{page}"
    subprocess.run(["pdftoppm", "-png", "-f", str(page), "-l", str(page),
                    "-scale-to-x", str(W), "-scale-to-y", str(H), PDF, str(dst)],
                   check=True)
    im = Image.open(sorted(OUT.glob(f"_raw{page}-*.png"))[0]).convert("RGB")
    return im


def pt(v):                 # points -> pixels (the deck's page is 960 x 540 pt)
    return round(v * 2)


def clear(im, box_pt, fill):
    x0, y0, x1, y1 = box_pt
    ImageDraw.Draw(im).rectangle([pt(x0), pt(y0), pt(x1), pt(y1)], fill=fill)


def clear_pill(im, x0, y0, x1, y1, fill):
    """Repaint a stadium exactly over the pill, so digits that run into its
    rounded ends go without the ends going with them."""
    ImageDraw.Draw(im).rounded_rectangle([x0, y0, x1, y1],
                                         radius=(y1 - y0) / 2, fill=fill)


p1, p2, p3 = render(1), render(2), render(3)

CREAM = p3.getpixel((pt(40), pt(120)))
PILL = p3.getpixel((pt(545), pt(262)))
MEDAL = p3.getpixel((pt(210), pt(410)))
print("sampled:", {"cream": CREAM, "pill": PILL, "medallion": MEDAL})

# --------------------------------------------------------------------------- #
# POST PAGE
# --------------------------------------------------------------------------- #
post = p3.copy()

# The handle sits between the "Handle Name" label (ink ends 153.3pt) and the
# "Social Media Report" line (ink starts 185.7pt) — clear only that band, or
# the art loses a line.
clear(post, (40, 155.5, 355, 184.6), CREAM)      # handle
clear(post, (130, 206.5, 285, 228), CREAM)       # date
# "N Posts" in the medallion. The band must stay INSIDE the circle: at y=394
# the circle spans x 188..291, so 194..286 is safe and still wide enough for a
# two-digit count.
clear(post, (194, 368, 286, 394), MEDAL)

for (x0, y0, x1, y1) in ((1072, 506, 1274, 566),      # Like
                         (1072, 588, 1274, 646),      # Post Impression
                         (1072, 676, 1274, 736)):     # Video Views
    clear_pill(post, x0, y0, x1, y1, PILL)

# the word LINK only — inside the chip's white plate, icon untouched
LINK_BLUE = p3.getpixel((1160, 790))
ImageDraw.Draw(post).rectangle([1148, 762, 1242, 812], fill=WHITE)

clear(post, (768, 94, 906, 148), WHITE)          # "Top N Posts" + "Post N"
clear(post, (658, 166, 914, 540), WHITE)         # the screenshot slot

# --- the platform logo: rebuild the photo edge it sits on ------------------- #
def photo_edge_fit(im):
    """x = m*y + c for the photo's right edge, from the rows above the logo."""
    a = im.load()
    pts = []
    for y in range(110, 165):
        for x in range(1700, 1100, -1):
            c = a[x, y]
            if all(abs(v - 255) <= 12 for v in c):
                continue
            if c[0] > 200 and c[1] < 130 and c[2] < 90:      # the orange ribbon
                continue
            pts.append((y, x))
            break
    n = len(pts)
    my = sum(p[0] for p in pts) / n
    mx = sum(p[1] for p in pts) / n
    m = (sum((p[0] - my) * (p[1] - mx) for p in pts)
         / sum((p[0] - my) ** 2 for p in pts))
    return m, mx - m * my, max(abs(m * y + (mx - m * my) - x) for y, x in pts)


m, c0, resid = photo_edge_fit(p3)
print(f"photo edge: x = {m:.3f}y + {c0:.1f}  (max residual {resid:.1f}px)")

src = p3.copy()                       # clone source: the untouched page
CLONE_DX = 70
for y in range(pt(78), pt(240)):
    edge = m * y + c0
    for x in range(1290, 1560):
        if x <= edge:
            post.putpixel((x, y), src.getpixel((max(0, x - CLONE_DX), y)))
        elif post.getpixel((x, y)) != WHITE:
            post.putpixel((x, y), WHITE)

post.save(OUT / "post.png", "PNG", optimize=True)

# --------------------------------------------------------------------------- #
# COVER
# --------------------------------------------------------------------------- #
cover = p1.copy()
# Both rects stop just past the ink: the green diagonal runs at x>=1060px
# behind the title, and Modi's sleeve starts at x=1085px beside the date.
clear(cover, (85, 268, 527, 345), WHITE)         # "Kashi Report"
clear(cover, (233, 381, 538, 427), WHITE)        # "10 August 2026"
cover.save(OUT / "cover.png", "PNG", optimize=True)

# --------------------------------------------------------------------------- #
# SUMMARY
# --------------------------------------------------------------------------- #
summary = p2.copy()
# The whole table INCLUDING its outer rule — the app draws its own.
clear(summary, (337, 240, 945, 536), WHITE)
clear(summary, (130, 206.5, 285, 228), CREAM)    # the date
summary.save(OUT / "summary.png", "PNG", optimize=True)

for f in OUT.glob("_raw*.png"):
    f.unlink()
print("LINK blue sampled:", LINK_BLUE)
for name in ("post", "cover", "summary"):
    p = OUT / f"{name}.png"
    print(f"  {name}.png  {Image.open(p).size}  {p.stat().st_size // 1024} KB")

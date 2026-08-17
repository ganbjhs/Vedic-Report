#!/usr/bin/env python3
"""End-to-end builder test on SYNTHETIC screenshots. Zero captures.

Real PNGs at realistic master sizes are generated with PIL, so `prof_builder`
runs its whole path — composite, place, draw — and actually produces PDF, DOCX
and HTML files. That exercises reportlab and python-docx for real without
touching X or the rate-limited capture account (RULEBOOK rule 21).

What it cannot prove: that the documents LOOK right. Rule 3 is not satisfied by
this file — a human still has to open one output per profile once. What it does
prove is that every profile builds, every declared output appears, page counts
follow the grid, demoted statuses are excluded, and the composite pipeline does
not corrupt an image.

    .venv/bin/python profiles/tests/test_builder.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "profiles"))

from PIL import Image                     # noqa: E402
import layout                             # noqa: E402
import prof_builder                       # noqa: E402
import registry                           # noqa: E402

FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          ("" if ok else f": got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


HEIGHTS = [832, 844, 784, 431, 491, 407, 176, 868]


def make_fixture(root: Path, n=8, dpr=1, heights=None):
    """Synthetic masters + a results.json in the shape the runners emit,
    including two demoted links that must NOT reach the document."""
    shots = root / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    results = []
    hs = heights or HEIGHTS
    for i in range(n):
        w, h = 598 * dpr, hs[i % len(hs)] * dpr
        im = Image.new("RGB", (w, h), "#FFFFFF")
        for y in range(0, h, 9):
            for x in range(0, w, 13):
                im.putpixel((x, y), ((x * 7) % 256, (y * 3) % 256, 160))
        path = shots / f"{i + 1:02d}_post.png"
        im.save(path)
        results.append({
            "url": f"https://x.com/u{i}/status/{1000 + i}",
            "status": "ok", "handle": f"@user{i}",
            "screenshot": str(path), "text": "hello",
            "overlay": False, "frame_ok": True, "parent_lost": False,
            "account_name": f"Account {i}", "category": "Uncategorized",
            "post_link": f"https://x.com/u{i}/status/{1000 + i}",
            "metrics": {"followers": f"{1000 + i}", "reactions": "12",
                        "comments": "3", "reach": "9000", "shares": "4"},
        })
    # Demoted links — the builder must drop both without any per-status logic.
    results.append({"url": "u", "status": "parent_lost", "screenshot": None,
                    "account_name": "Orphan", "post_link": "https://x.com/o/status/1"})
    results.append({"url": "u", "status": "overlay_blocked", "screenshot": None,
                    "account_name": "Blocked", "post_link": "https://x.com/b/status/2"})
    (root / "results.json").write_text(json.dumps(results))
    return results


def build(slug, root, title="Test Report"):
    prof_builder.OUT = root
    sys.argv = ["prof_builder", title, "Test_Report", slug]
    prof_builder.main()
    return {p.suffix.lstrip("."): p for p in root.glob("Test_Report.*")}


# --------------------------------------------------------------------------- #
print("\n1. every profile builds every output it declares")
for slug in registry.available():
    profile = registry.load(slug)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        make_fixture(root)
        made = build(slug, root)
        for kind in profile["outputs"]:
            check(f"{slug}: produced .{kind}", kind in made)
            if kind in made:
                check(f"{slug}: .{kind} is non-trivial",
                      made[kind].stat().st_size > 900)

print("\n2. demoted links are excluded, with no per-status logic")
check("parent_lost is not usable",
      prof_builder.usable({"status": "parent_lost", "screenshot": __file__}), False)
check("overlay_blocked is not usable",
      prof_builder.usable({"status": "overlay_blocked", "screenshot": __file__}), False)
check("age_restricted is not usable",
      prof_builder.usable({"status": "age_restricted", "screenshot": __file__}), False)
check("ok with a real file is usable",
      prof_builder.usable({"status": "ok", "screenshot": __file__}), True)
check("ok with a missing file is not",
      prof_builder.usable({"status": "ok", "screenshot": "/nope/x.png"}), False)

print("\n3. page count follows the grid")
for slug, expect in (("twitter", 8), ("influencer", 4), ("contact-sheet", 2),
                     ("client-deck", 8)):
    p = registry.load(slug)
    check(f"{slug}: 8 posts -> {expect} page(s)", layout.page_count(p, 8), expect)

print("\n4. the composite pipeline preserves the image (no corruption)")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    results = [r for r in make_fixture(root) if prof_builder.usable(r)]
    for slug in ("twitter", "client-deck", "contact-sheet"):
        profile = registry.load(slug)
        with tempfile.TemporaryDirectory() as wd:
            images, places = prof_builder.prepare(results, profile, wd)
            check(f"{slug}: one image per post", len(images), len(results))
            check(f"{slug}: one placement per post", len(places), len(results))
            ok_open = True
            for img in images:
                try:
                    with Image.open(img) as im:
                        im.verify()
                except Exception:
                    ok_open = False
            check(f"{slug}: every composite is a readable JPEG", ok_open)
            box_w, box_h = profile["image"]["max_in"]
            check(f"{slug}: no placement exceeds its declared box",
                  all(p.w_in <= box_w + 1e-6 and p.h_in <= box_h + 1e-6
                      for p in places), True)
            pw, ph = registry.page_inches(profile["page"])
            check(f"{slug}: every placement is on the page",
                  all(0 <= p.x_in and p.x_in + p.w_in <= pw + 1e-6
                      and 0 <= p.y_in and p.y_in + p.h_in <= ph + 1e-6
                      for p in places), True)

print("\n5. client-deck actually reshapes to 4:5 (the aspect is applied)")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    results = [r for r in make_fixture(root) if prof_builder.usable(r)]
    with tempfile.TemporaryDirectory() as wd:
        images, _ = prof_builder.prepare(results, registry.load("client-deck"), wd)
        ratios = []
        for img in images:
            with Image.open(img) as im:
                ratios.append(im.size[0] / im.size[1])
        # Padded to 4:5 (0.8) then grown slightly by border+shadow, so it lands
        # near 0.8 rather than exactly on it.
        check("every card is close to 4:5",
              all(0.74 < r < 0.88 for r in ratios), True)

print("\n6. DPR 2 masters produce the SAME placements as DPR 1")
with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
    r1 = Path(td1); r2 = Path(td2)
    a = [r for r in make_fixture(r1, dpr=1) if prof_builder.usable(r)]
    b = [r for r in make_fixture(r2, dpr=2) if prof_builder.usable(r)]
    profile = registry.load("twitter")
    with tempfile.TemporaryDirectory() as w1, tempfile.TemporaryDirectory() as w2:
        _, pa = prof_builder.prepare(a, profile, w1)
        _, pb = prof_builder.prepare(b, profile, w2)
    check("placements identical at DPR 1 and DPR 2",
          [p.as_tuple() for p in pa], [p.as_tuple() for p in pb])

print("\n7. PAGE-COUNT PARITY against the frozen builders")
# The geometry parity suite compares image PLACEMENTS and is blind to anything
# else on the page. That blind spot shipped a real defect: the links table was
# forced onto its own page, making the twitter profile 9 pages against the
# frozen builder's 8. Page count is the cheapest end-to-end check that sees it.
import re as _re, zlib as _zlib                # noqa: E402


def pdf_pages(path):
    raw = Path(path).read_bytes()
    n = len(_re.findall(rb"/Type\s*/Page[^s]", raw))
    if n:
        return n
    total = 0
    for m in _re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, _re.S):
        try:
            total += len(_re.findall(rb"/Type\s*/Page[^s]",
                                     _zlib.decompress(m.group(1))))
        except Exception:
            pass
    if total:
        return total
    c = _re.search(rb"/Type\s*/Pages.*?/Count\s+(\d+)", raw, _re.S)
    return int(c.group(1)) if c else -1


# WHAT PARITY MEANS HERE, precisely. The agreed bar is GEOMETRY parity: same
# page count for the SCREENSHOT pages and the same placement for every image
# (asserted in test_parity.py). The links table is a trailing appendix and
# reportlab's platypus flows it by its own rules, which this canvas-based
# builder does not reimplement — so its exact page may differ by one from the
# frozen builder's. That is a documented cosmetic difference, NOT a licence for
# the table to consume a page gratuitously, which is what these two cases pin
# down: with room below the last post it MUST share that page.
for slug in ("twitter", "influencer"):
    profile = registry.load(slug)
    for label, heights, slack in (("short last post", [176], 0),
                                  ("tall last post", [868], 1)):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [r for r in make_fixture(root, n=6, heights=heights)
                    if prof_builder.usable(r)]
            made = build(slug, root)
            shot_pages = layout.page_count(profile, len(rows))
            total = pdf_pages(made["pdf"])
            check(f"{slug} / {label}: {total} page(s) vs {shot_pages} of "
                  f"screenshots (slack {slack})",
                  shot_pages <= total <= shot_pages + slack, True)

print("\n8. an empty / all-demoted results.json does not crash")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "results.json").write_text(json.dumps(
        [{"status": "parent_lost", "screenshot": None, "post_link": "u"}]))
    try:
        build("twitter", root)
        check("handled with no output and no exception",
              list(root.glob("Test_Report.*")), [])
    except Exception as e:
        check(f"handled cleanly (raised {type(e).__name__}: {e})", False)

print("\n9. the design kit: the Canva guide and the live preview")
# What these pin down is AGREEMENT. The guide and the preview are a second
# implementation of tpl_builder's drawing rules in PIL (there is no rasteriser
# in this project), so the thing worth asserting is that they land a slot where
# the PDF lands it — a guide that disagreed would be worse than no guide.
import tpl_preview                          # noqa: E402
from PIL import Image as _Image             # noqa: E402

_tpl = registry.load("combined-16x9")
W, H, ppp = tpl_preview.page_pixels(_tpl)
check("16:9 landscape renders at 1920x1080", (W, H), (1920, 1080))
check("points scale with the page", round(ppp, 4), round(144 / 72, 4))
for size, orient, want in (("a4", "portrait", (1240, 1754)),
                           ("a4", "landscape", (1754, 1240)),
                           ("letter", "portrait", (1275, 1650)),
                           ("4:3", "landscape", (1440, 1080))):
    p = json.loads(json.dumps(_tpl))
    p["page"]["size"], p["page"]["orientation"] = size, orient
    check(f"{size} {orient} -> {want[0]}x{want[1]}",
          tpl_preview.page_pixels(p)[:2], want)

guide = tpl_preview.guide_png(_tpl, "post")
with tempfile.TemporaryDirectory() as td:
    gp = Path(td) / "g.png"
    gp.write_bytes(guide)
    with _Image.open(gp) as g:
        check("the guide is a transparent RGBA layer", g.mode, "RGBA")
        check("the guide is the page's pixel size", g.size, (W, H))
        alpha = g.split()[3]
        check("the guide is mostly transparent (art shows through)",
              alpha.getextrema()[0], 0)
        check("the guide actually draws something", alpha.getextrema()[1], 255)

# The slot the guide outlines is the slot the PDF fills.
pw_in, _ = registry.page_inches(_tpl["page"])
scale = W / pw_in
for i, (idx, _c, sx, sy, sw, sh) in enumerate(layout.slot_boxes(_tpl)):
    frac = _tpl["template"]["slots"][i]
    check(f"slot {i}: guide px == builder inches x{scale:.0f}",
          (round(frac["x"] * W), round(frac["y"] * H)),
          (round(sx * scale), round(sy * scale)))

page_png = tpl_preview.page_png(_tpl, "post")
with tempfile.TemporaryDirectory() as td:
    pp = Path(td) / "p.png"
    pp.write_bytes(page_png)
    with _Image.open(pp) as im:
        check("the preview is the page's pixel size", im.size, (W, H))
        check("the preview is not a blank page",
              len(im.convert("RGB").getcolors(maxcolors=200000) or []) > 50, True)
check("a summary page renders too", len(tpl_preview.page_png(_tpl, "summary")) > 5000, True)

print("\n10. template fonts are validated, never silently dropped")


def bad_profile(mutate, why):
    p = json.loads(json.dumps(_tpl))
    mutate(p)
    try:
        registry.validate(p)
        check(f"refused: {why}", False)
    except registry.ProfileError:
        check(f"refused: {why}", True)


def ok_profile(mutate, why):
    p = json.loads(json.dumps(_tpl))
    mutate(p)
    try:
        registry.validate(p)
        check(f"accepted: {why}", True)
    except registry.ProfileError as e:
        check(f"accepted: {why} ({e})", False)


ok_profile(lambda p: p["template"].update(fonts=["Brand.ttf"]),
           "a style with one font and no slot using it")
ok_profile(lambda p: (p["template"].update(fonts=["Brand.otf"]),
                      p["template"]["text"][0].update(font="Brand.otf")),
           "a text slot naming a font the style ships")
bad_profile(lambda p: p["template"]["text"][0].update(font="Brand.ttf"),
            "a text slot naming a font the style does NOT ship")
bad_profile(lambda p: p["template"].update(fonts=["a.ttf", "b.ttf", "c.ttf", "d.ttf"]),
            "a fourth font")
bad_profile(lambda p: p["template"].update(fonts=["../../etc/passwd.ttf"]),
            "a font path instead of a filename")
bad_profile(lambda p: p["template"].update(fonts=["Brand.exe"]),
            "a font that is not .ttf/.otf")
bad_profile(lambda p: p["template"].update(fonts=["a.ttf", "a.ttf"]),
            "the same font twice")

# A font file that is not there must fall back to Helvetica and SAY so, rather
# than take the build down (rule 17).
missing = json.loads(json.dumps(_tpl))
missing["template"]["fonts"] = ["Nope.ttf"]
missing["template"]["text"][0]["font"] = "Nope.ttf"
try:
    import tpl_builder                      # noqa: E402
    got = tpl_builder.register_fonts(missing)
    check("a missing font file registers nothing and does not raise", got, {})
    check("its text slot falls back to Helvetica",
          tpl_builder._pdf_font(missing["template"]["text"][0], got), "Helvetica")
except Exception as e:
    check(f"missing font handled cleanly (raised {type(e).__name__}: {e})", False)

# End to end with a REAL font file, through the whole builder.
_face = None
for cand in tpl_preview._FONT_CANDIDATES:
    if Path(cand).exists():
        _face = Path(cand)
        break
if _face is None:
    print("  SKIP  no system TTF on this machine to test embedding with")
else:
    with tempfile.TemporaryDirectory() as reg, tempfile.TemporaryDirectory() as td:
        reg = Path(reg)
        (reg / "assets" / "fontstyle" / "fonts").mkdir(parents=True)
        (reg / "assets" / "fontstyle" / "fonts" / "Brand.ttf").write_bytes(_face.read_bytes())
        _Image.new("RGB", (1920, 1080), "#FFFFFF").save(
            reg / "assets" / "fontstyle" / "post.png")
        p = json.loads(json.dumps(_tpl))
        p.update(slug="fontstyle", label="Font style", outputs=["pdf", "html"])
        p["template"]["pages"] = {"post": "post.png"}
        p["template"]["fonts"] = ["Brand.ttf"]
        p["template"]["text"] = [dict(p["template"]["text"][1], font="Brand.ttf")]
        (reg / "fontstyle.json").write_text(json.dumps(p))
        registry.set_user_dir(reg)
        try:
            root = Path(td)
            make_fixture(root, n=2)
            made = build("fontstyle", root)
            check("a style with an uploaded font builds a PDF", "pdf" in made)
            # reportlab writes the TTF's OWN name (subset-tagged, e.g.
            # "AAAAAA+ArialMT"), not the name it was registered under — so the
            # evidence of embedding is the embedded font FILE, /FontFile2.
            raw = made["pdf"].read_bytes()
            check("the font file is embedded in the PDF",
                  b"/FontFile2" in raw and b"/TrueType" in raw, True)
            check("the HTML inlines it as @font-face",
                  "@font-face" in made["html"].read_text(errors="ignore"), True)
        finally:
            registry.set_user_dir(None)

# --------------------------------------------------------------------------- #
print("\n11. non-Latin text picks a font that can draw it (rule 14)")
# A combined report prints the name the CAPTURE read, and a Varanasi Facebook
# Page is called "काशी के मोदी". Helvetica is WinAnsi-encoded, so drawing that
# in Helvetica is the black-rectangle bug rule 14 is about.
import tpl_builder                             # noqa: E402

check("Helvetica is kept for Latin text",
      tpl_builder._drawable("The Chaupal", "Helvetica"), "Helvetica")
check("Helvetica is kept for cp1252 text (é, ·)",
      tpl_builder._drawable("Café · Banaras", "Helvetica"), "Helvetica")
check("Helvetica cannot draw Devanagari",
      tpl_builder._fits_font("काशी के मोदी", "Helvetica"), False)

swapped = tpl_builder._drawable("काशी के मोदी", "Helvetica")
uni = tpl_builder._unicode_font()
if uni:
    check("Devanagari swaps to the registered Unicode font", swapped, uni[0])
    check("...which really carries those glyphs",
          all(ord(ch) in uni[1] for ch in "काशी के मोदी"), True)
else:
    # A machine with no Unicode TTF must still build — and must SAY so rather
    # than swap to something that cannot draw it either (rule 17).
    check("with no Unicode font installed the request font is kept",
          swapped, "Helvetica")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("BUILDER OK — all profiles build; rule 3 still needs human eyes once")

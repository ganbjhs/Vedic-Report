#!/usr/bin/env python3
"""Unit tests for profiles/shapes.py and profiles/registry.py. Zero captures.

The load-bearing assertion is the FIRST one: for the two existing report types,
`compose` must be a bit-for-bit identity. If passing a master through the
profile engine can alter a single pixel, the engine is not safe to put in front
of a working report.

    .venv/bin/python profiles/tests/test_shapes.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "profiles"))

from PIL import Image                 # noqa: E402
import registry                       # noqa: E402
import shapes                         # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def raises(name, fn, needle=""):
    try:
        fn()
    except registry.ProfileError as e:
        ok = needle.lower() in str(e).lower()
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {str(e)[:88]}")
        if not ok:
            FAILS.append(name)
        return
    except Exception as e:
        print(f"  FAIL  {name}: wrong exception {type(e).__name__}: {e}")
        FAILS.append(name)
        return
    print(f"  FAIL  {name}: did not raise")
    FAILS.append(name)


def master(w=598, h=832):
    """A stand-in with real structure, so an accidental resize is visible."""
    im = Image.new("RGB", (w, h), "#FFFFFF")
    for y in range(0, h, 7):
        for x in range(0, w, 11):
            im.putpixel((x, y), ((x * 3) % 256, (y * 5) % 256, 128))
    return im


# --------------------------------------------------------------------------- #
print("\n1. THE SAFETY PROPERTY: existing profiles pass through untouched")
im = master()
for slug in ("twitter", "influencer"):
    spec = registry.load(slug)["image"]
    check(f"{slug}: is_identity", shapes.is_identity(spec), True)
    out = shapes.compose(im, spec, placement_w_in=4.9)
    check(f"{slug}: same object returned", out is im, True)
    check(f"{slug}: pixels identical", out.tobytes() == im.tobytes(), True)

# --------------------------------------------------------------------------- #
print("\n2. is_identity() detects every op that would change a pixel")
check("radius", shapes.is_identity({"radius_pt": 8}), False)
check("border", shapes.is_identity({"border": {"pt": 1}}), False)
check("shadow", shapes.is_identity({"shadow": {"blur_pt": 9, "opacity": 0.2}}), False)
check("zero-opacity shadow is a no-op",
      shapes.is_identity({"shadow": {"blur_pt": 9, "opacity": 0}}), True)
check("aspect+pad", shapes.is_identity({"aspect": "4:5", "fit": "pad"}), False)
check("aspect without pad/crop", shapes.is_identity({"aspect": "4:5", "fit": "fit"}), True)
check("watermark", shapes.is_identity({"watermark": "x"}), False)
check("empty spec", shapes.is_identity({}), True)

# --------------------------------------------------------------------------- #
print("\n3. pad_to_aspect never crops")
tall = master(598, 900)
padded = shapes.pad_to_aspect(tall.convert("RGBA"), 4 / 5, "#FFFFFF")
check("width grew to reach 4:5", padded.size[0] > tall.size[0], True)
check("height unchanged (no crop)", padded.size[1], tall.size[1])
check("ratio is now 4:5", round(padded.size[0] / padded.size[1], 3), round(4 / 5, 3))

wide = master(900, 400)
padded_w = shapes.pad_to_aspect(wide.convert("RGBA"), 4 / 5, "#FFFFFF")
check("wide input pads height", padded_w.size[1] > wide.size[1], True)
check("wide input keeps width", padded_w.size[0], wide.size[0])

# --------------------------------------------------------------------------- #
print("\n4. decoration is in POINTS at placement size — DPR-invariant")
spec = {"radius_pt": 12, "border": {"pt": 1, "color": "#E1E8ED"},
        "background": "#FFFFFF"}
lo = shapes.compose(master(598, 832), spec, placement_w_in=4.9)
hi = shapes.compose(master(1196, 1664), spec, placement_w_in=4.9)
# The border grows the image by the same number of POINTS at both DPRs, so the
# growth as a FRACTION of width must match.
grow_lo = (lo.size[0] - 598) / 598
grow_hi = (hi.size[0] - 1196) / 1196
check("border grows by the same fraction at DPR 1 and 2",
      abs(grow_lo - grow_hi) < 0.004, True)
check("DPR 2 output is still ~2x the DPR 1 output",
      abs(hi.size[0] / lo.size[0] - 2.0) < 0.02, True)

# --------------------------------------------------------------------------- #
print("\n5. ops that grow the canvas do so predictably")
im2 = master(200, 200).convert("RGBA")
b = shapes.bordered(im2, 4, "#FF0000")
check("border adds 2x width", b.size, (208, 208))
s = shapes.shadowed(im2, 6, 0.3, 3)
check("shadow grows the canvas", s.size[0] > 200 and s.size[1] > 200, True)
check("flatten returns RGB", shapes.flatten(im2).mode, "RGB")
r = shapes.rounded(im2, 20)
check("rounded keeps size", r.size, (200, 200))
check("rounded punches the corner alpha", r.getpixel((0, 0))[3] < 40, True)
check("rounded keeps the centre opaque", r.getpixel((100, 100))[3], 255)

# --------------------------------------------------------------------------- #
print("\n6. registry rejects what would render wrongly or silently")
raises("unknown key", lambda: registry.validate(
    {**registry.load("twitter"), "image": {**registry.load("twitter")["image"],
                                           "radius-px": 3}}), "unknown key")
raises("thread_ancestors names the real reason", lambda: registry.validate(
    {**registry.load("twitter"),
     "capture": {**registry.load("twitter")["capture"], "thread_ancestors": 2}}),
       "approved edit 6")
raises("metrics on the x engine", lambda: registry.validate(
    {**registry.load("twitter"),
     "content": {**registry.load("twitter")["content"],
                 "metrics": [["Followers", "followers"]]}}), "never produces metrics")
raises("xlsx as a profile output", lambda: registry.validate(
    {**registry.load("twitter"), "outputs": ["pdf", "xlsx"]}), "global data export")
raises("narrow viewport (rule 4)", lambda: registry.validate(
    {**registry.load("twitter"),
     "capture": {**registry.load("twitter")["capture"],
                 "viewport": {"width": 800, "height": 1600}}}), ">= 900")
raises("pad without an aspect", lambda: registry.validate(
    {**registry.load("twitter"),
     "image": {**registry.load("twitter")["image"], "fit": "pad"}}), "needs an aspect")
raises("missing max_in", lambda: registry.validate(
    {**registry.load("twitter"),
     "image": {k: v for k, v in registry.load("twitter")["image"].items()
               if k != "max_in"}}), "max_in")
raises("bad schema version", lambda: registry.validate(
    {**registry.load("twitter"), "schema": 99}), "schema must be 1")
raises("unknown profile", lambda: registry.load("nope"), "no such profile")

print("\n7. extends merges per-section and keeps the child's identity")
import json, tempfile                       # noqa: E402
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    base = json.loads((ROOT / "profiles" / "registry" / "twitter.json").read_text())
    (d / "twitter.json").write_text(json.dumps(base))
    (d / "deck.json").write_text(json.dumps({
        "schema": 1, "slug": "deck", "label": "Deck", "extends": "twitter",
        "image": {"aspect": "4:5", "fit": "pad", "radius_pt": 9}}))
    deck = registry.load("deck", d)
    check("child slug wins", deck["slug"], "deck")
    check("child label wins", deck["label"], "Deck")
    check("inherited page size", deck["page"]["size"], "letter")
    check("inherited max_in", deck["image"]["max_in"], [4.9, 7.0])
    check("overridden aspect", deck["image"]["aspect"], "4:5")
    check("no longer an identity", shapes.is_identity(deck["image"]), False)
    (d / "loop.json").write_text(json.dumps(
        {"schema": 1, "slug": "loop", "label": "L", "extends": "loop"}))
    raises("circular extends", lambda: registry.load("loop", d), "circular")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL SHAPES + REGISTRY CHECKS PASSED")

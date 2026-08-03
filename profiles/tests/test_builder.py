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


def make_fixture(root: Path, n=8, dpr=1):
    """Synthetic masters + a results.json in the shape the runners emit,
    including two demoted links that must NOT reach the document."""
    shots = root / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    results = []
    for i in range(n):
        w, h = 598 * dpr, HEIGHTS[i % len(HEIGHTS)] * dpr
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

print("\n7. an empty / all-demoted results.json does not crash")
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

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("BUILDER OK — all profiles build; rule 3 still needs human eyes once")

#!/usr/bin/env python3
"""metrics/shot_metrics.py — the screenshot reader, without a browser.

    .venv/bin/python profiles/tests/test_shot_metrics.py

Three layers, each skipping cleanly when its prerequisite is missing:

1. The PARSER, on hand-built OCR words — no tesseract needed. Number forms,
   labelled lines for every platform, Facebook's counts row, X's slot map, and
   the two promises (typed wins, unshown stays blank).
2. OCR on RENDERED mock-ups of the Facebook / Instagram rows — needs tesseract
   and a TrueType font. There are no Facebook or Instagram captures in the
   repo, so this is what stands in for them until a real run supplies some.
3. OCR on the REAL X captures under data/acceptance/influencer-1/, whose
   results.json holds the numbers the DOM reader saw at capture time. Every
   value the picture shows must be read back, to the precision X printed it.
   Skipped when that folder is not on this machine.

Zero captures, no network, no X account.
"""
import io
import json
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))                      # webapp.* (section 7)
sys.path.insert(0, str(ROOT / "metrics"))
sys.path.insert(0, str(ROOT / "profiles"))

import shot_metrics as SM                          # noqa: E402

FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          ("" if ok else f": got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def W(text, x, y=10, w=None, h=12, conf=95):
    return SM.Word(text, x, y, w if w is not None else 7 * len(text), h, conf)


def row(text, x0=16, conf=95, y=10):
    """One OCR line from a string, words spaced 60px apart."""
    return [W(t, x0 + i * 60, y=y, conf=conf) for i, t in enumerate(text.split())]


def shown(found):
    return {k: v[1] for k, v in found.items()}


# --------------------------------------------------------------------------- #
print("\n1. numbers as the platforms print them")
for raw, want in [("984", 984), ("1,234", 1234), ("3,275", 3275), ("1.1K", 1100),
                  ("1,1K", 1100), ("63K", 63000), ("418.2K", 418200), ("1.2M", 1200000),
                  ("2.1 lakh", 210000), ("1.5 crore", 15000000), ("३,२७५", 3275),
                  ("12 K", 12000), ("1.234", 1234), ("1.5", None), ("abc", None),
                  ("", None), (None, None)]:
    check(f"to_int({raw!r}) == {want}", SM.to_int(raw), want)
check("compact(1156) is X's 1.2K", SM.compact(1156), "1.2K")
check("compact(None) is the dash", SM.compact(None), SM.MISSING)

# --------------------------------------------------------------------------- #
print("\n2. labelled lines — any platform")
f = SM.parse_labelled([row("10:45 AM - Aug 1, 2026 - 3,275 Views")])
check("X views line", shown(f), {"views": "3,275"})
f = SM.parse_labelled([row("1,234 likes"), row("View all 56 comments", y=40)])
check("Instagram likes + 'View all N comments'", shown(f),
      {"likes": "1,234", "comments": "56"})
f = SM.parse_labelled([row("12.3K views", y=10), row("2.1 lakh likes", y=40)])
check("Instagram reel views + lakh likes", shown(f),
      {"views": "12.3K", "likes": "2.1 lakh"})
f = SM.parse_labelled([row("ee 644 45 comments 11 shares")])
check("Facebook counts row (glyphs OCR'd as letters in front)", shown(f),
      {"likes": "644", "comments": "45", "shares": "11"})
f = SM.parse_labelled([row("2.3K - 120 comments - 1.1K shares")])
check("Facebook counts row, compact numbers", shown(f),
      {"likes": "2.3K", "comments": "120", "shares": "1.1K"})
f = SM.parse_labelled([row("45 comments 11 shares")])
check("Facebook row with no reactions count leaves likes blank", shown(f),
      {"comments": "45", "shares": "11"})
f = SM.parse_labelled([row("Ravi and 642 others · 45 comments")])
check("'N others' is the reactions count", shown(f)["likes"], "642")
f = SM.parse_labelled([row("1.2K followers")])
check("followers", shown(f), {"followers": "1.2K"})
f = SM.parse_labelled([row("Sold 45 units, 3 views of the sea"),
                       row("9:00 PM - Jul 1, 2026 - 18 Views", y=60)])
check("two 'views' lines: the LOWER one wins (the reply is underneath)",
      shown(f)["views"], "18")
f = SM.parse_labelled([row("Rally draws 50,000 people, ministers say")])
check("a number without a metric word is not a metric", shown(f), {})

# --------------------------------------------------------------------------- #
print("\n3. X's action bar — slots by position, never by order")
for frac, want in [(0.03, "comments"), (0.14, "comments"), (0.26, "shares"),
                   (0.34, "shares"), (0.48, "likes"), (0.54, "likes"),
                   (0.71, "slot4"), (0.75, "slot4"), (0.93, "share"), (0.40, None)]:
    check(f"x={frac:.2f} -> {want}", SM._slot(frac), want)
width = 600.0
bar = [W("Os:", 21, y=181, h=16, conf=44), W("td", 153, y=183, h=13, conf=20),
       W("01", 289, y=182, h=14, conf=49), W("|", 425, y=181, h=16, conf=12),
       W("4,", 558, y=182, h=14, conf=51)]
body = row("Issue VFX ka nahi hai issue ye hai", y=80)
views = row("10:45 AM - Aug 1, 2026 - 3,275 Views", y=134)
rows = SM.lines_of(bar + body + views)
band = SM.find_x_bar(rows, width, text_h=12)
check("the icon row is found by its shape", band is not None and 175 < band[0] < 185)
check("a text line is never taken for the bar",
      SM.find_x_bar([body, views], width, text_h=12), None)
two = SM.lines_of(bar + [W(t, x, y=400, h=16, conf=30) for t, x in
                         (("©", 65), ("108", 87), ("111", 181), ("0", 299),
                          ("11K", 322), ("tht", 418), ("63K", 438))])
band = SM.find_x_bar(two, width, text_h=12)
check("two bars in one picture: the LOWER one is read", band is not None and band[0] > 390)

# --------------------------------------------------------------------------- #
print("\n4. filling a job's results.json — typed wins, unshown stays blank")


def fake_read(shot, link="", platform="auto", engine="tesseract"):
    r = SM._blank(shot, link, SM.platform_of(link, platform), "ok", engine)
    for k, (n, s) in {"likes": (644, "644"), "comments": (45, "45")}.items():
        r[k] = n
        r["shown"][k] = s
        r["display"][k] = SM.compact(n)
    if "empty" in str(shot):
        return SM._blank(shot, link, platform, "no_numbers", engine)
    return r


tmp = Path(tempfile.mkdtemp())
try:
    shots = []
    for name in ("a.png", "b.png", "empty.png"):
        p = tmp / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        shots.append(str(p))
    results = [
        {"status": "ok", "screenshot": shots[0], "post_link": "https://www.facebook.com/p/1",
         "platform": "facebook", "sheet_metrics": {"like": "700", "impressions": "9,100"}},
        {"status": "ok", "screenshot": shots[1], "post_link": "https://www.instagram.com/p/x/",
         "platform": "instagram"},
        {"status": "ok", "screenshot": shots[2], "post_link": "https://x.com/a/status/1",
         "platform": "x", "sheet_metrics": {"views": "50"}},
        {"status": "login_wall", "screenshot": None, "post_link": "https://x.com/a/status/2",
         "platform": "x"},
        # what the page reader leaves behind on an X row: every target filled
        {"status": "ok", "screenshot": shots[0], "post_link": "https://x.com/a/status/3",
         "platform": "x", "sheet_metrics": {"like": "1", "comments": "2", "shares": "3",
                                            "views": "4", "reach": "4", "impressions": "4"}},
    ]
    rp = tmp / "results.json"
    rp.write_text(json.dumps(results))
    real = SM.read_image
    SM.read_image = fake_read
    buf = io.StringIO()
    with redirect_stdout(buf):
        filled = SM.fill_results(rp)
    SM.read_image = real
    out = json.loads(rp.read_text())
    check("cells filled: a.comments + b.likes + b.comments", filled, 3)
    check("a typed 'like' is untouched", out[0]["sheet_metrics"]["like"], "700")
    check("a typed 'impressions' is untouched", out[0]["sheet_metrics"]["impressions"], "9,100")
    check("a blank 'comments' is filled as SHOWN", out[0]["sheet_metrics"]["comments"], "45")
    check("row with no sheet metrics gets both", out[1]["sheet_metrics"],
          {"like": "644", "comments": "45"})
    check("a picture that shows no number changes nothing", out[2].get("sheet_metrics"),
          {"views": "50"})
    check("a row without a screenshot is left alone", "sheet_metrics" not in out[3])
    check("a row the page reader already filled is not read again (and unchanged)",
          out[4]["sheet_metrics"], {"like": "1", "comments": "2", "shares": "3",
                                    "views": "4", "reach": "4", "impressions": "4"})
    check("'shares' never written as 0 when not shown",
          all("shares" not in (r.get("sheet_metrics") or {}) for r in out[:4]))
    check("metrics_read.json written", (tmp / "metrics_read.json").is_file())
    check("metrics_read.csv written", (tmp / "metrics_read.csv").is_file())
    lines = buf.getvalue().splitlines()
    check("progress: reading line", any(l.startswith("[shots] reading 3 screenshot") for l in lines))
    check("progress: per-shot lines",
          sum(1 for l in lines if re.match(r"^\[shots\] \d+/3 ", l)), 3)
    check("progress: filled line", any(l.startswith("[shots] filled 3 blank cell(s) from 2/3") for l in lines))
    csv_head = (tmp / "metrics_read.csv").read_text(encoding="utf-8").splitlines()[0]
    check("csv carries the shown-as columns", "Likes (shown)" in csv_head and "Evidence" in csv_head)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------- #
print("\n5. OCR on rendered Facebook / Instagram rows")
if not SM.tesseract_path():
    print("  SKIP  tesseract is not installed here")
else:
    try:
        from PIL import Image, ImageDraw, ImageFont
        font = small = None
        for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/System/Library/Fonts/Supplemental/Arial.ttf",
                     "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf"):
            if Path(cand).is_file():
                font, small = ImageFont.truetype(cand, 15), ImageFont.truetype(cand, 13)
                break
    except Exception:
        font = None
    if font is None:
        print("  SKIP  no TrueType font to render the mock-ups with")
    else:
        tmp = Path(tempfile.mkdtemp())
        try:
            fb = Image.new("RGB", (600, 200), "white")
            d = ImageDraw.Draw(fb)
            d.text((16, 12), "Kashi Ke Wasi", fill="black", font=font)
            d.text((16, 40), "Dev Deepawali at Assi Ghat tonight.", fill=(50, 50, 50), font=font)
            for i, c in enumerate([(24, 119, 242), (240, 60, 60), (250, 190, 40)]):
                d.ellipse((16 + i * 12, 120, 32 + i * 12, 136), fill=c)
            d.text((58, 118), "644", fill=(100, 100, 100), font=small)
            d.text((330, 118), "45 comments", fill=(100, 100, 100), font=small)
            d.text((470, 118), "11 shares", fill=(100, 100, 100), font=small)
            d.text((60, 160), "Like", fill=(100, 100, 100), font=small)
            d.text((260, 160), "Comment", fill=(100, 100, 100), font=small)
            d.text((460, 160), "Share", fill=(100, 100, 100), font=small)
            fb.save(tmp / "fb.png")
            ig = Image.new("RGB", (600, 160), "white")
            d = ImageDraw.Draw(ig)
            d.text((16, 20), "1,234 likes", fill="black", font=font)
            d.text((16, 60), "naliniskitchen  Paneer butter masala", fill=(50, 50, 50), font=small)
            d.text((16, 90), "View all 56 comments", fill=(120, 120, 120), font=small)
            d.text((16, 120), "2 DAYS AGO", fill=(150, 150, 150), font=small)
            ig.save(tmp / "ig.png")
            r = SM.read_image(tmp / "fb.png", "https://www.facebook.com/x/posts/1")
            check("facebook: reactions · comments · shares",
                  {k: r["shown"][k] for k in ("likes", "comments", "shares")},
                  {"likes": "644", "comments": "45", "shares": "11"})
            check("facebook: no views invented", r["views"], None)
            r = SM.read_image(tmp / "ig.png", "https://www.instagram.com/p/abc/")
            check("instagram: likes + comments",
                  {k: r["shown"][k] for k in ("likes", "comments")},
                  {"likes": "1,234", "comments": "56"})
            check("instagram: '2 DAYS AGO' is not a metric", r["views"], None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------- #
print("\n6. the real X captures under data/acceptance/influencer-1/")
fixture = ROOT / "data" / "acceptance" / "influencer-1" / "reports" / "results.json"
if not SM.tesseract_path():
    print("  SKIP  tesseract is not installed here")
elif not fixture.is_file():
    print(f"  SKIP  {fixture.relative_to(ROOT)} is not on this machine")
else:
    # the DOM reader's vocabulary -> this reader's
    MAP = {"comments": "comments", "shares": "shares", "reactions": "likes", "reach": "views"}
    total = right = 0
    for r in json.loads(fixture.read_text(encoding="utf-8")):
        shot = fixture.parent / "screenshots" / Path(r["screenshot"]).name
        if not shot.is_file():
            continue
        truth = (r.get("metrics") or {}).get("_raw") or {}
        got = SM.read_image(shot, r.get("url", ""), "x")
        bad = []
        for src, key in MAP.items():
            want = truth.get(src) or None          # 0 in the DOM = nothing printed
            g = got[key]
            # the picture prints X's compact form (1,156 -> "1.1K"), so a read
            # within that rounding is the picture's own value
            ok = (want is None and g is None) or (
                want is not None and g is not None and
                (g == want or (want >= 1000 and abs(g - want) / want < 0.1)))
            total += 1
            right += ok
            if not ok:
                bad.append(f"{key}: read {got['shown'][key]} vs {want}")
        check(f"{shot.name}" + (f"  {bad}" if bad else ""), not bad)
    check(f"fields read back from the pictures: {right}/{total}", right == total)

# --------------------------------------------------------------------------- #
print("\n7. the Kashi deck: a read count lands in Likes and Post Reach, nowhere else")
import registry                                    # noqa: E402  (profiles/)

kashi = registry.load("kashi-deck-16x9")
KMAP = registry.read_metrics_map(kashi)
check("the deck names its map", KMAP, {"likes": ["like"], "views": ["reach"]})
pills = {t["field"] for t in kashi["template"]["text"] if t["field"].startswith("metric.")}
check("its pills are the five labelled ones",
      pills, {"metric.like", "metric.views", "metric.reach", "metric.impressions",
              "metric.shares"})
check("every map target is a pill the deck draws",
      all(f"metric.{k}" in pills for cols in KMAP.values() for k in cols))

# the screenshot reader, with the deck's map
tmp = Path(tempfile.mkdtemp())
try:
    shot = tmp / "fb.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    results = [{"status": "ok", "screenshot": str(shot), "platform": "facebook",
                "post_link": "https://www.facebook.com/kashi/posts/1",
                "sheet_metrics": {"impressions": "18234"}}]
    rp = tmp / "results.json"
    rp.write_text(json.dumps(results))

    def rich_read(shot_, link="", platform="auto", engine="tesseract"):
        r = SM._blank(shot_, link, "facebook", "ok", engine)
        for k, (n, sh) in {"likes": (644, "644"), "comments": (45, "45"),
                           "shares": (11, "11"), "views": (12300, "12.3K")}.items():
            r[k] = n
            r["shown"][k] = sh
            r["display"][k] = SM.compact(n)
        return r

    real = SM.read_image
    SM.read_image = rich_read
    buf = io.StringIO()
    with redirect_stdout(buf):
        n = SM.fill_results(rp, sheet_map=KMAP)
    SM.read_image = real
    got = json.loads(rp.read_text())[0]["sheet_metrics"]
    check("two cells filled", n, 2)
    check("Likes <- likes, Post Reach <- views, typed Impressions kept, no Video "
          "views / ReTweets / comments invented",
          got, {"impressions": "18234", "like": "644", "reach": "12.3K"})
    # and with the default map the same picture fills the wide set
    rp.write_text(json.dumps(results))
    SM.read_image = rich_read
    with redirect_stdout(buf):
        SM.fill_results(rp)
    SM.read_image = real
    got = json.loads(rp.read_text())[0]["sheet_metrics"]
    check("default map: like, comments, shares, views, reach; typed impressions kept",
          got, {"impressions": "18234", "like": "644", "comments": "45",
                "shares": "11", "views": "12.3K", "reach": "12.3K"})
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 'hidden': the word the deck prints where a post shows no view count
tmp = Path(tempfile.mkdtemp())
try:
    shots = {}
    for name in ("photo.png", "video.png", "typed.png", "blank.png", "unread.png"):
        (tmp / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        shots[name] = str(tmp / name)
    results = [
        # a Facebook photo post: reactions and comments, no view count anywhere
        {"status": "ok", "screenshot": shots["photo.png"], "platform": "facebook",
         "post_link": "https://www.facebook.com/kashi/posts/1"},
        # a video post: views shown -> the number, never the word
        {"status": "ok", "screenshot": shots["video.png"], "platform": "facebook",
         "post_link": "https://www.facebook.com/kashi/videos/2"},
        # the team typed a reach -> untouched
        {"status": "ok", "screenshot": shots["typed.png"], "platform": "facebook",
         "post_link": "https://www.facebook.com/kashi/posts/3",
         "sheet_metrics": {"reach": "18234"}},
        # a picture with no readable number at all -> still 'hidden'
        {"status": "ok", "screenshot": shots["blank.png"], "platform": "instagram",
         "post_link": "https://www.instagram.com/p/4/"},
        # OCR could not run -> we do not know -> nothing written
        {"status": "ok", "screenshot": shots["unread.png"], "platform": "instagram",
         "post_link": "https://www.instagram.com/p/5/"},
    ]
    rp = tmp / "results.json"
    rp.write_text(json.dumps(results))

    def fb_read(shot_, link="", platform="auto", engine="tesseract"):
        n = Path(shot_).name
        if n == "unread.png":
            return SM._blank(shot_, link, platform, "no_ocr", engine)
        if n == "blank.png":
            return SM._blank(shot_, link, platform, "no_numbers", engine)
        r = SM._blank(shot_, link, "facebook", "ok", engine)
        vals = {"likes": (644, "644"), "comments": (45, "45")}
        if n == "video.png":
            vals["views"] = (12300, "12.3K")
        for k, (v, sh) in vals.items():
            r[k] = v
            r["shown"][k] = sh
            r["display"][k] = SM.compact(v)
        return r

    real = SM.read_image
    SM.read_image = fb_read
    buf = io.StringIO()
    with redirect_stdout(buf):
        SM.fill_results(rp, sheet_map=KMAP, missing=registry.read_missing_map(kashi))
    SM.read_image = real
    out = json.loads(rp.read_text())
    check("photo post: Likes 644, Post Reach 'hidden'",
          out[0]["sheet_metrics"], {"like": "644", "reach": "hidden"})
    check("video post: Post Reach is the number", out[1]["sheet_metrics"],
          {"like": "644", "reach": "12.3K"})
    check("typed reach is kept", out[2]["sheet_metrics"], {"reach": "18234", "like": "644"})
    check("no number anywhere in the picture: still 'hidden'",
          out[3]["sheet_metrics"], {"reach": "hidden"})
    check("no OCR: nothing written (we do not know)", out[4].get("sheet_metrics"), None)
    # a word for a count the map sends nowhere is ignored, not written somewhere
    rp.write_text(json.dumps(results))
    SM.read_image = fb_read
    with redirect_stdout(io.StringIO()):
        SM.fill_results(rp, sheet_map=KMAP, missing={"comments": "hidden"})
    SM.read_image = real
    check("a 'missing' text for a key the map does not send anywhere is ignored",
          [r.get("sheet_metrics") for r in json.loads(rp.read_text())][:2],
          [{"like": "644"}, {"like": "644", "reach": "12.3K"}])
    rp.write_text(json.dumps(results))
    SM.read_image = fb_read
    with redirect_stdout(buf):
        SM.fill_results(rp, sheet_map=KMAP, missing=registry.read_missing_map(kashi))
    SM.read_image = real
    read_json = json.loads((tmp / "metrics_read.json").read_text())
    photo = next(r for r in read_json if r["screenshot"].endswith("photo.png"))
    check("the CSV/JSON evidence says why the word is there",
          any("not shown -> reach 'hidden'" in e for e in photo["evidence"]))
    check("the log says how many cells got the word",
          any(l.startswith("[shots] 2 cell(s) marked as not shown") for l in buf.getvalue().splitlines()))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# the X page reader's merge in the web layer, with the same map
try:
    from webapp.jobs import runner as RN                 # noqa: E402
    from webapp import report_types as RT                # noqa: E402
except Exception as e:                                   # no web deps here
    print(f"  SKIP  web layer not importable here ({str(e)[:60]})")
else:
    rt = RT.get("kashi-deck-16x9")
    check("ReportType carries the deck's map",
          rt is not None and dict((k, list(v)) for k, v in rt.read_metrics_map), KMAP)
    check("a style without a map carries the default",
          dict((k, list(v)) for k, v in RT.get("combined-16x9").read_metrics_map),
          registry.DEFAULT_READ_METRICS)
    x_row = {"link": "https://x.com/a/status/1", "status": "ok", "likes": 1156,
             "reposts": 111, "replies": 108, "views": 63954, "bookmarks": 49,
             "display": {"likes": "1.2K", "reposts": "111", "replies": "108",
                         "views": "63K", "bookmarks": "49"}}
    rows = [{"link": "https://x.com/a/status/1", "sheet_metrics": {"shares": "typed"}}]
    n = RN._merge_metrics(rows, [x_row], rt.read_metrics_map)
    check("page reader + Kashi map: Likes and Post Reach only", (n, rows[0]["sheet_metrics"]),
          (2, {"shares": "typed", "like": "1.2K", "reach": "63K"}))
    rows = [{"link": "https://x.com/a/status/1", "sheet_metrics": {}}]
    n = RN._merge_metrics(rows, [x_row])
    check("page reader + default map: the wide set, as before",
          (n, rows[0]["sheet_metrics"]),
          (6, {"like": "1.2K", "comments": "108", "shares": "111", "views": "63K",
               "reach": "63K", "impressions": "63K"}))

# --------------------------------------------------------------------------- #
print("\n8. the Handle Name is the display name, never the @username (profiles/names.py)")
import names                                        # noqa: E402  (profiles/)


class FakePage:
    def __init__(self, title="", header="", og=""):
        self._t, self._h, self._og = title, header, og

    def title(self):
        return self._t

    def evaluate(self, js, arg=None):
        return self._og if "og:title" in js else self._h


check("X title -> display name",
      names.display_name(FakePage('Yogi Adityanath on X: "On 27, 28 and 29 August…" / X'), "x",
                         "@myogiadityanath"), "Yogi Adityanath")
check("Instagram title -> display name",
      names.display_name(FakePage("Nalini's Kitchen on Instagram: \"Paneer…\""), "instagram",
                         "@naliniskitchen"), "Nalini's Kitchen")
check("X: title not in shape -> the focused tweet's header, first line",
      names.display_name(FakePage("Post / X", header="Banaras Live"), "x", "@banaraslive"),
      "Banaras Live")
check("Instagram: title not in shape -> og:title",
      names.display_name(FakePage("Instagram", og="Kashi Ke Wasi on Instagram: \"…\""),
                         "instagram"), "Kashi Ke Wasi")
check("emoji and flags are stripped (Helvetica has no glyph for them)",
      names.clean("Yogi Adityanath 🇮🇳 🪷"), "Yogi Adityanath")
check("Devanagari survives", names.clean("काशी के मोदी"), "काशी के मोदी")
check("a bare @handle is not a name", names.clean("@someone"), "")
check("facebook is left to its engine (already the Page name)",
      names.display_name(FakePage("Banaras Live | Facebook"), "facebook"), "")
check("fallback: the handle WITHOUT its '@'", names.account_name("", "@myogiadityanath"),
      "myogiadityanath")
check("display name wins over the handle", names.account_name("Yogi Adityanath", "@x"),
      "Yogi Adityanath")
check("nothing read -> nothing (the placeholder stands)", names.account_name("", ""), "")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("SCREENSHOT READER OK — reads what the picture shows, fills only blanks")

#!/usr/bin/env python3
"""Read the engagement numbers a SCREENSHOT shows — likes, comments, shares,
views — with no login, no account and no page visit.

    python metrics/shot_metrics.py shot.png
    python metrics/shot_metrics.py shot1.png shot2.jpg --platform facebook
    python metrics/shot_metrics.py reports/results.json --fill
    python metrics/shot_metrics.py reports/screenshots/ --json read.json --csv read.csv

WHY THIS EXISTS. `metrics/x_metrics.py` reads a post's numbers off the live
page, and it needs the shared X account to do it. Facebook and Instagram never
gave us that route: a logged-out visitor is shown the login sheet, and there is
no shared account to sign in with (RULEBOOK §18b). But every capture this tool
takes already HAS the numbers in the picture — the "644 · 45 comments · 11
shares" row under a Facebook post, "1,234 likes" under an Instagram one, the
reply · repost · like · views bar under an X post. This module reads them back
out of the pixels, so the report can print what the screenshot shows without
anyone typing it.

WHAT IT IS, AND IS NOT. It reads what the picture shows — the PUBLIC counts.
Insights numbers (impressions, reach as the platform's dashboard reports them)
are not on a public post and are not read here; the sheet's columns for those
stay whatever the team typed (RULEBOOK §18c rule 10 still holds). And the same
two promises `x_metrics` makes hold here:

  * A number typed into the sheet always wins. Only blank cells are filled.
  * A number the picture does not show stays blank. Never 0.

WHERE A NUMBER GOES is the style's decision. By default likes -> like, comments
-> comments, shares -> shares and the one public view count -> views, reach AND
impressions (the sheet heads that column "Reach/views"). A style whose pills
are labelled names its own `read_metrics` map in its profile — the Kashi deck
says {"likes": ["like"], "views": ["reach"]}, so a read count lands in its
"Likes" and "Post Reach" pills and nowhere else. `run_profile.py` passes the
map in; the CLI takes `--map 'likes=like;views=reach'`.

HOW IT READS. Tesseract (the `tesseract` binary, found on PATH — no Python
package) in two passes:

  1. Whole image, 2x. Every LABELLED count — "3,275 Views", "45 comments",
     "11 shares", "1,234 likes", "View all 12 comments", "12.3K views" — is
     read straight off its line, on any platform. Facebook's reactions count is
     the bare number that opens the "N · N comments · N shares" row.
  2. X's action bar only. It carries icons and numbers, no words, so the line
     is found by its shape (tokens at the five icon positions), the icons are
     erased (they are the tall connected components in that band), and the
     band is re-read at 4x with a digit-only vocabulary. Each number is then
     assigned to a slot by WHERE it sits — reply · repost · like · views /
     bookmark — because X shows nothing at all for a zero count, so counting
     tokens left to right would shift every number after a missing one.

A screenshot that holds two posts (a reply shot with its parent) has two bars;
the LOWER one is read, because the reply — the post the link points at — is
always the one underneath.

OPTIONAL SECOND ENGINE. With `XAI_API_KEY` set (docs/v3-plan.md §4),
`--engine grok` sends the picture to Grok's vision endpoint and asks for the
same fields as JSON. Wired, not yet exercised against a live key here; any
failure prints why and falls back to tesseract, so it can never take a report
down. Tesseract is the default and the tested path.

Output — one dict per screenshot:

    {"screenshot", "link", "platform", "status", "engine",
     "likes", "comments", "shares", "views", "followers", "bookmarks",
     "shown": {key: "1.1K"},       # the text exactly as the picture shows it
     "display": {key: "1.1K"},     # X's compact form of the parsed integer
     "evidence": ["likes 1.1K <- action bar slot 3", ...]}

`status`: "ok" (at least one number read) | "no_numbers" | "no_screenshot" |
"no_ocr" (tesseract not installed) | "error: …". A value that could not be
read is None and its display string is "—".

Progress goes out through `profiles/progress.py`, never a bare print():
`webapp/jobs/runner.py` regex-matches these lines to drive the job page, and
`profiles/tests/test_progress_contract.py` holds both sides to it.
"""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_prof = str(ROOT / "profiles")
if _prof not in sys.path:
    sys.path.insert(0, _prof)

try:
    import progress as P                                  # profiles/, noqa: E402
except Exception:                                         # standalone use
    P = None

MISSING = "—"
KEYS = ("likes", "comments", "shares", "views", "followers", "bookmarks")

# What the picture can say -> the sheet's own metric keys
# (profiles/netlinks.py METRIC_HEADERS). This is the DEFAULT, identical to
# `registry.DEFAULT_READ_METRICS` and to what x_metrics fills: views, reach and
# impressions are ONE public number — the sheet heads that column
# "Reach/views" — so a read view count fills whichever of the three is blank.
# A style may say otherwise with its own `read_metrics` map (the Kashi deck
# sends likes to its "Likes" pill and views to "Post Reach", nothing else);
# `fill_results(..., sheet_map=…)` takes it from `run_profile.py`.
SHEET_MAP = (("likes", ("like",)),
             ("comments", ("comments",)),
             ("shares", ("shares",)),
             ("views", ("views", "reach", "impressions")))


# --------------------------------------------------------------------------- #
# Numbers as platforms print them
# --------------------------------------------------------------------------- #
_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
_MAG = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
        "thousand": 1_000, "lakh": 100_000, "lac": 100_000, "crore": 10_000_000,
        "million": 1_000_000, "billion": 1_000_000_000,
        "हज़ार": 1_000, "हजार": 1_000, "लाख": 100_000, "करोड़": 10_000_000,
        "करोड": 10_000_000}
# "1,234"  "1.1K"  "63K"  "1.2M"  "1,1K" (OCR comma for point)  "1.1 lakh"
_NUM_RE = re.compile(
    r"(?<![\w.])(\d[\d,.]*)\s*(K|M|B|thousand|lakh|lac|crore|million|billion|"
    r"हज़ार|हजार|लाख|करोड़|करोड)?(?![\w])", re.I)
_TOKEN_RE = re.compile(r"^\d[\d,.]*[KMB]?$", re.I)


def to_int(raw: str):
    """'12,431' / '12.4K' / '1,1K' / '1.1 lakh' / '३,२७५' -> int, else None."""
    if raw is None:
        return None
    s = str(raw).translate(_DEVANAGARI).strip()
    m = re.match(r"^(\d[\d,.]*)\s*([A-Za-zऀ-ॿ]*)$", s)
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).lower()
    mult = _MAG.get(suffix, None) if suffix else 1
    if mult is None:
        return None
    if mult > 1:
        # Under a magnitude any separator is a decimal point: 1,1K == 1.1K.
        num = num.replace(",", ".")
        if num.count(".") > 1:
            return None
    else:
        # A plain number: commas group thousands; a lone dot followed by
        # exactly three digits is a European thousands separator, anything
        # else with a dot is not a count.
        num = num.replace(",", "")
        if "." in num:
            if re.fullmatch(r"\d{1,3}\.\d{3}", num):
                num = num.replace(".", "")
            else:
                return None
    try:
        return int(round(float(num) * mult))
    except ValueError:
        return None


def compact(n) -> str:
    """X's own display style: 984, 1.2K, 45K, 1.2M (same as inf_capture)."""
    if n is None:
        return MISSING
    n = int(n)
    if n < 1_000:
        return str(n)
    if n < 10_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    if n < 1_000_000:
        return f"{n // 1_000}K"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{n // 1_000_000}M"


# --------------------------------------------------------------------------- #
# OCR — the tesseract binary, through stdin/stdout
# --------------------------------------------------------------------------- #
class Word:
    __slots__ = ("text", "x", "y", "w", "h", "conf")

    def __init__(self, text, x, y, w, h, conf):
        self.text, self.x, self.y, self.w, self.h, self.conf = text, x, y, w, h, conf

    @property
    def cy(self):
        return self.y + self.h / 2.0

    def __repr__(self):
        return f"Word({self.text!r} @{self.x},{self.y} {self.w}x{self.h} {self.conf:.0f})"


def tesseract_path():
    return os.environ.get("TESSERACT_BIN") or shutil.which("tesseract")


def _tsv_words(img, scale: int, psm: int, lang: str, whitelist: str = "") -> list:
    """Run tesseract on a PIL image; words with boxes scaled back to 1x."""
    from PIL import Image
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    cmd = [tesseract_path(), "stdin", "stdout", "--psm", str(psm), "-l", lang, "tsv"]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    out = subprocess.run(cmd, input=buf.getvalue(), capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or b"").decode("utf-8", "replace").strip()[:300])
    words = []
    for line in out.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) != 12 or not cols[11].strip():
            continue
        try:
            x, y, w, h = (int(cols[i]) for i in (6, 7, 8, 9))
            conf = float(cols[10])
        except ValueError:
            continue
        words.append(Word(cols[11].strip(), x / scale, y / scale, w / scale,
                          h / scale, conf))
    return words


def _langs() -> str:
    """eng, plus hin when that traineddata is installed (Facebook's Hindi UI
    labels its counts 'कमेंट' / 'शेयर')."""
    want = os.environ.get("TESSERACT_LANGS")
    if want:
        return want
    try:
        out = subprocess.run([tesseract_path(), "--list-langs"], capture_output=True,
                             timeout=20).stdout.decode("utf-8", "replace")
        have = {l.strip() for l in out.splitlines()}
    except Exception:
        have = set()
    return "eng+hin" if "hin" in have else "eng"


def _prepare(path):
    """Greyscale, and light-on-dark flipped to dark-on-light: tesseract wants
    black text on white, and a dark-mode capture is the other way round."""
    from PIL import Image, ImageOps, ImageStat
    im = Image.open(path)
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    im = im.convert("L")
    if ImageStat.Stat(im).mean[0] < 110:
        im = ImageOps.invert(im)
    return ImageOps.autocontrast(im)


def lines_of(words: list, tol: float = 0.6) -> list:
    """Group words into text lines by vertical centre; each line left->right."""
    rows = []
    for w in sorted(words, key=lambda w: (w.cy, w.x)):
        for row in rows:
            ref = row[-1]
            if abs(w.cy - ref.cy) <= tol * max(ref.h, w.h, 1):
                row.append(w)
                break
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w.x)
    rows.sort(key=lambda r: sum(w.cy for w in r) / len(r))
    return rows


def _line_text(row) -> str:
    return " ".join(w.text for w in row)


# --------------------------------------------------------------------------- #
# Pass 1 — labelled counts, any platform
# --------------------------------------------------------------------------- #
_LABELS = {
    "views":     r"views?|plays?|impressions?|व्यूज़|व्यूज|व्यू|बार देखा गया",
    "likes":     r"likes?|reactions?|लाइक्स|लाइक|पसंद",
    "comments":  r"comments?|replies|reply|कमेंट्स|कमेंट|टिप्पणियाँ|टिप्पणी",
    "shares":    r"shares?|reposts?|retweets?|शेयर",
    "followers": r"followers?|फ़ॉलोअर्स|फॉलोअर्स|फॉलोअर",
    "bookmarks": r"bookmarks?",
}
_LABELLED = {k: re.compile(r"(?:^|[^\w])(?:view\s+all\s+)?" + _NUM_RE.pattern +
                           r"\s*(?:" + v + r")(?![\w])", re.I)
             for k, v in _LABELS.items()}
# Facebook's counts row: "644 · 45 comments · 11 shares" — the reactions count
# is the one number in that row with no word beside it, only the reaction
# glyphs (which OCR as a letter or two, hence no anchor at the line start).
_FB_ROW_LABEL = re.compile(r"\b(?:comments?|shares?|कमेंट|शेयर)\b", re.I)
_ANY_LABEL = re.compile(r"^\W*(?:" + "|".join(_LABELS.values()) + r")(?![\w])", re.I)


def _clean_ocr_text(s: str) -> str:
    s = s.translate(_DEVANAGARI)
    # OCR's usual slips inside a number, only when digits sit on both sides.
    s = re.sub(r"(?<=\d)[oO](?=[\d,.]|$)", "0", s)
    return s


def parse_labelled(rows: list) -> dict:
    """{key: (int, shown, evidence)} from every line that names its count."""
    found = {}
    for row in rows:
        text = _clean_ocr_text(_line_text(row))
        for key, rx in _LABELLED.items():
            for m in rx.finditer(text):
                shown = re.sub(r"\s+", " ", m.group(1) + (" " + m.group(2) if m.group(2) else "")).strip()
                if len(m.group(2) or "") == 1:
                    shown = shown.replace(" ", "")      # 1.1 K -> 1.1K
                n = to_int(shown)
                if n is None:
                    continue
                # keep the LOWEST occurrence: in a two-post shot the reply is
                # underneath, and it is the post the link points at.
                found[key] = (n, shown, f"{key} {shown} <- line {text.strip()!r}")
        if _FB_ROW_LABEL.search(text):
            for m in _NUM_RE.finditer(text):
                if _ANY_LABEL.match(text[m.end():]):
                    continue                    # that number has its own word
                if not _FB_ROW_LABEL.search(text[m.end():]):
                    break                       # past the comments/shares part
                shown = m.group(0).strip()
                n = to_int(shown)
                if n is not None and "likes" not in found:
                    found["likes"] = (n, shown, f"likes {shown} <- counts row {text.strip()!r}")
                break
    return found


# --------------------------------------------------------------------------- #
# Pass 2 — X's action bar: icons and numbers, no words
# --------------------------------------------------------------------------- #
# Where each group's left edge sits, as a fraction of the post width. Two
# layouts exist — the detail page spreads reply · repost · like · bookmark ·
# share edge to edge, the timeline card packs reply · repost · like · views
# with bookmark+share at the right — and these ranges cover both.
_SLOTS = (("comments", 0.00, 0.19),     # reply
          ("shares", 0.21, 0.38),       # repost
          ("likes", 0.42, 0.58),        # like
          ("slot4", 0.63, 0.80),        # views (card) or bookmark (detail)
          ("share", 0.84, 1.01))        # share — never carries a number


def _slot(x_frac: float):
    for name, lo, hi in _SLOTS:
        if lo <= x_frac < hi:
            return name
    return None


def _text_height(words: list) -> float:
    """Typical height of a confidently-read word — the body text size."""
    hs = sorted(w.h for w in words if w.conf >= 75 and len(w.text) >= 2)
    if not hs:
        hs = sorted(w.h for w in words) or [12.0]
    return hs[len(hs) // 2]


def find_x_bar(rows: list, width: float, text_h: float):
    """The lowest line whose tokens sit at >= 3 of the five icon positions and
    that carries no real word — X's action bar. None when there is none."""
    best = None
    for row in rows:
        if any(w.conf >= 80 and len(w.text) >= 4 and w.text.isalpha() for w in row):
            continue
        slots = {_slot(w.x / width) for w in row}
        slots.discard(None)
        if len(slots) < 3:
            continue
        span = (row[-1].x + row[-1].w) - row[0].x
        if span < 0.5 * width:
            continue
        top = min(w.y for w in row)
        bottom = max(w.y + w.h for w in row)
        if bottom - top > 3.5 * text_h:            # not one line of glyphs
            continue
        best = (top, bottom)
    return best


def _components(px, w, h, ink):
    """Connected components of `ink` pixels in a small band. Pure Python on a
    600x30 crop — a few thousand pixels, not worth a numpy dependency."""
    seen = bytearray(w * h)
    comps = []
    for y0 in range(h):
        for x0 in range(w):
            i = y0 * w + x0
            if seen[i] or px[i] >= ink:
                continue
            stack = [i]
            seen[i] = 1
            minx = maxx = x0
            miny = maxy = y0
            while stack:
                j = stack.pop()
                yy, xx = divmod(j, w)
                minx, maxx = min(minx, xx), max(maxx, xx)
                miny, maxy = min(miny, yy), max(maxy, yy)
                for dy in (-1, 0, 1):
                    ny = yy + dy
                    if ny < 0 or ny >= h:
                        continue
                    for dx in (-1, 0, 1):
                        nx = xx + dx
                        if nx < 0 or nx >= w:
                            continue
                        k = ny * w + nx
                        if not seen[k] and px[k] < ink:
                            seen[k] = 1
                            stack.append(k)
            comps.append((minx, miny, maxx - minx + 1, maxy - miny + 1))
    return comps


def read_x_bar(img, band, text_h: float, lang: str) -> dict:
    """{slot_key: (int, shown)} — the numbers in X's action bar by position."""
    from PIL import Image, ImageDraw
    top, bottom = band
    pad = int(round(0.5 * text_h))
    y0 = max(0, int(top) - pad)
    y1 = min(img.height, int(bottom) + pad + 1)
    crop = img.crop((0, y0, img.width, y1))
    w, h = crop.size
    comps = _components(crop.tobytes(), w, h, ink=150)
    # The digits in the bar share one height, and the icons are the tallest
    # things in it (about 1.5x a digit). So: the commonest height among the
    # components clearly shorter than the tallest is the digit height. A bar
    # with every count at zero has no such cluster — and nothing to read.
    hmax = max((ch for (_, _, _, ch) in comps), default=0)
    hs = Counter(ch for (_, _, cw, ch) in comps
                 if 3 <= ch < 0.85 * hmax and cw >= 2)
    if not hs:
        return {}
    digit_h, count = hs.most_common(1)[0]
    if count < 2:
        # a lone digit ("1 like", nothing else) — accept it only if the one
        # component is digit-shaped, not a stray piece of an icon
        lone = [c for c in comps if c[3] == digit_h and 0.25 * digit_h <= c[2] <= 0.9 * digit_h]
        if not lone:
            return {}
    draw = ImageDraw.Draw(crop)
    for (cx, cy, cw, ch) in comps:
        icon = (ch >= 1.25 * digit_h                              # taller than a digit
                or (cw <= 0.25 * digit_h and ch >= 0.5 * digit_h)  # a bar of the views glyph
                or (cw >= 1.2 * digit_h and ch <= 0.5 * digit_h))  # a wide, flat stroke
        if icon:
            draw.rectangle((cx - 1, cy - 1, cx + cw, cy + ch), fill=255)
    words = _tsv_words(crop, scale=4, psm=7, lang=lang, whitelist="0123456789.,KMB")
    if len([wd for wd in words if _TOKEN_RE.match(wd.text)]) == 0:
        words = _tsv_words(crop, scale=4, psm=6, lang=lang, whitelist="0123456789.,KMB")
    if os.environ.get("SHOT_METRICS_DEBUG"):
        print(f"[shots] bar digit_h={digit_h} tokens={words}", flush=True)
    out = {}
    for wd in words:
        tok = _clean_ocr_text(wd.text).strip("., ")
        if not _TOKEN_RE.match(tok):
            continue
        n = to_int(tok)
        if n is None or n == 0:            # X never prints a zero
            continue
        # A lone character whose box is not digit-height is a piece of icon
        # read as a digit. Only lone characters: tesseract 4 pads a real
        # "111" or "481" box with leftover pixels (15px tall for 9px digits)
        # while reading the text itself perfectly well.
        if len(tok) == 1 and not (0.75 * digit_h <= wd.h <= 1.3 * digit_h):
            continue
        slot = _slot(wd.x / w)
        if slot in (None, "share"):
            continue
        # two tokens in one slot (a broken "1.1K"): keep the one with letters
        if slot in out and not re.search(r"[KMB]$", tok, re.I):
            continue
        out[slot] = (n, tok.upper())
    return out


# --------------------------------------------------------------------------- #
# Optional engine — Grok vision (docs/v3-plan.md §4)
# --------------------------------------------------------------------------- #
_GROK_PROMPT = (
    "This is a screenshot of one social-media post (X/Twitter, Facebook or "
    "Instagram). Read the engagement numbers exactly as the picture prints them. "
    "Reply with ONLY a JSON object with these keys: likes, comments, shares, "
    "views, followers, bookmarks. Each value is the text as shown (for example "
    "\"1.1K\", \"3,275\", \"644\") or null when the picture does not show that "
    "number. On X: replies are comments, reposts are shares. If two posts are "
    "visible, read the LOWER one. Never guess and never write 0 for a number "
    "that is not shown.")


def read_with_grok(path) -> dict:
    """{key: (int, shown)} from Grok's vision endpoint. Raises on any failure —
    the caller prints why and falls back to tesseract."""
    import base64
    import urllib.request
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("XAI_API_KEY is not set")
    model = (os.environ.get("XAI_VISION_MODEL") or os.environ.get("XAI_MODEL")
             or "grok-4")
    data = Path(path).read_bytes()
    mime = "image/jpeg" if str(path).lower().endswith((".jpg", ".jpeg")) else "image/png"
    body = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                "detail": "high"}},
            {"type": "text", "text": _GROK_PROMPT}]}],
    }
    req = urllib.request.Request(
        os.environ.get("XAI_API_URL", "https://api.x.ai/v1/chat/completions"),
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        reply = json.loads(r.read().decode("utf-8"))
    text = reply["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"no JSON in reply: {text[:120]!r}")
    got = json.loads(m.group(0))
    out = {}
    for k in KEYS:
        v = got.get(k)
        if v in (None, "", 0, "0", "null"):
            continue
        n = to_int(str(v))
        if n is not None and n > 0:
            out[k] = (n, str(v).strip())
    return out


# --------------------------------------------------------------------------- #
# One screenshot
# --------------------------------------------------------------------------- #
def platform_of(link: str, hint: str = "") -> str:
    if hint and hint != "auto":
        return hint
    u = (link or "").lower()
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "facebook"
    if "instagram.com" in u:
        return "instagram"
    if "x.com" in u or "twitter.com" in u:
        return "x"
    return "auto"


def _blank(shot, link, platform, status, engine) -> dict:
    row = {"screenshot": str(shot or ""), "link": link or "", "platform": platform,
           "status": status, "engine": engine, "evidence": []}
    for k in KEYS:
        row[k] = None
    row["shown"] = {k: MISSING for k in KEYS}
    row["display"] = {k: MISSING for k in KEYS}
    return row


def read_image(shot, link: str = "", platform: str = "auto",
               engine: str = "tesseract") -> dict:
    """Engagement numbers shown in one screenshot. Never raises for content."""
    platform = platform_of(link, platform)
    if not shot or not Path(shot).is_file():
        return _blank(shot, link, platform, "no_screenshot", engine)

    found = {}
    used = "tesseract"
    if engine == "grok":
        try:
            found = {k: (n, s, f"{k} {s} <- grok") for k, (n, s) in
                     read_with_grok(shot).items()}
            used = "grok"
        except Exception as e:
            print(f"[shots] grok failed ({str(e)[:120]}) - falling back to tesseract",
                  flush=True)
            found = {}
    if not found:
        if not tesseract_path():
            return _blank(shot, link, platform, "no_ocr", "tesseract")
        try:
            found = _read_with_tesseract(shot, platform)
        except Exception as e:
            return _blank(shot, link, platform, f"error: {e}", "tesseract")
        used = "tesseract"

    row = _blank(shot, link, platform, "ok" if found else "no_numbers", used)
    for k, (n, shown, why) in found.items():
        row[k] = n
        row["shown"][k] = shown
        row["display"][k] = compact(n)
        row["evidence"].append(why)
    return row


def _reread_number(img, wd: Word, lang: str):
    """A second look at one doubtful number. Facebook's reaction glyphs sit
    against the reactions count and drag its OCR confidence down — "644" comes
    back as "44" at confidence 23. Crop the word with a margin, erase every
    blob taller than a digit (the glyphs), and read the digits alone at 4x."""
    from PIL import Image, ImageDraw
    m = max(2, int(wd.h * 0.6))
    box = (max(0, int(wd.x - 2.5 * wd.h)), max(0, int(wd.y - m)),
           min(img.width, int(wd.x + wd.w + m)), min(img.height, int(wd.y + wd.h + m)))
    crop = img.crop(box)
    w, h = crop.size
    if w < 4 or h < 4:
        return None
    draw = ImageDraw.Draw(crop)
    for (cx, cy, cw, ch) in _components(crop.tobytes(), w, h, ink=150):
        if ch >= 1.3 * wd.h or (cw >= 1.3 * wd.h and ch >= wd.h):
            draw.rectangle((cx - 1, cy - 1, cx + cw, cy + ch), fill=255)
    got = _tsv_words(crop, scale=4, psm=7, lang=lang, whitelist="0123456789.,KMB")
    toks = [_clean_ocr_text(g.text).strip("., ") for g in got]
    toks = [t for t in toks if _TOKEN_RE.match(t) and to_int(t) is not None]
    return toks[-1] if len(toks) == 1 else None


def _read_with_tesseract(shot, platform: str) -> dict:
    img = _prepare(shot)
    lang = _langs()
    words = _tsv_words(img, scale=2, psm=6, lang=lang)
    for wd in words:
        if wd.conf < 70 and re.search(r"\d", wd.text) and len(wd.text) <= 8:
            better = _reread_number(img, wd, lang)
            if better and better != wd.text:
                wd.text = better
    rows = lines_of(words)
    found = parse_labelled(rows)
    if platform in ("x", "auto"):
        text_h = _text_height(words)
        band = find_x_bar(rows, img.width, text_h)
        if band:
            bar = read_x_bar(img, band, text_h, lang)
            for slot, (n, shown) in bar.items():
                if slot == "slot4":
                    # the timeline card's 4th group is views; the detail page's
                    # is bookmarks — and the detail page prints views in words
                    key = "bookmarks" if "views" in found else "views"
                else:
                    key = slot
                if key in found and key != "views":
                    continue                    # a labelled read is the safer one
                if key == "views" and "views" in found:
                    continue
                found[key] = (n, shown, f"{key} {shown} <- action bar "
                                        f"{'slot4' if slot == 'slot4' else slot}")
    return found


# --------------------------------------------------------------------------- #
# Many screenshots — a folder, a list, or a job's results.json
# --------------------------------------------------------------------------- #
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def targets_from(items: list, platform: str) -> list:
    """[(shot, link, platform)] from image paths, folders, or results.json."""
    out = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in _IMG_EXT:
                    out.append((str(f), "", platform))
        elif p.suffix.lower() == ".json" and p.is_file():
            for r in json.loads(p.read_text(encoding="utf-8")):
                out.append((r.get("screenshot") or "", r.get("post_link") or
                            r.get("url") or "", r.get("platform") or platform))
        elif p.suffix.lower() in _IMG_EXT and p.is_file():
            out.append((str(p), "", platform))
        else:
            print(f"[shots] skipped {it}: not an image, folder or results.json",
                  flush=True)
    return out


def _say(fn, *a):
    if P is not None and hasattr(P, fn):
        getattr(P, fn)(*a)


def read_many(targets: list, engine: str = "tesseract") -> list:
    if not targets:
        print("[shots] nothing to read.", flush=True)
        return []
    if engine != "grok" and not tesseract_path():
        _say("shots_no_ocr")
        return [_blank(s, l, platform_of(l, p), "no_ocr", "tesseract")
                for s, l, p in targets]
    _say("shots_reading", len(targets))
    rows = []
    for n, (shot, link, plat) in enumerate(targets, start=1):
        row = read_image(shot, link, plat, engine)
        rows.append(row)
        s = row["shown"]
        _say("shots_one", n, len(targets), row["status"], s["likes"], s["comments"],
             s["shares"], s["views"], Path(shot).name if shot else "-")
    return rows


def parse_map(spec: str) -> list:
    """'likes=like;views=reach,impressions' -> [("likes", ("like",)), …]."""
    out = []
    for part in re.split(r"[;\s]+", (spec or "").strip()):
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"bad --map entry {part!r}; want read_key=sheet_key[,…]")
        src, cols = part.split("=", 1)
        src = src.strip().lower()
        if src not in KEYS:
            raise ValueError(f"unknown read key {src!r}; one of {list(KEYS)}")
        keys = tuple(c.strip() for c in cols.split(",") if c.strip())
        if not keys:
            raise ValueError(f"--map {src}= names no sheet key")
        out.append((src, keys))
    return out


def _as_map(sheet_map) -> list:
    """Accept the module default, a registry-style dict, or a list of pairs."""
    if not sheet_map:
        return list(SHEET_MAP)
    if isinstance(sheet_map, dict):
        return [(k, tuple(v)) for k, v in sheet_map.items()]
    return [(k, tuple(v)) for k, v in sheet_map]


def fill_results(results_path, engine: str = "tesseract", only_blank: bool = True,
                 json_out=None, csv_out=None, sheet_map=None, missing=None) -> int:
    """Read every screenshot in a job's results.json and fill in the blank
    sheet_metrics. Returns how many cells were filled. Writes results.json back
    in place, plus the full read beside it (metrics_read.json / .csv).

    `sheet_map` — where each read count goes: the style's `read_metrics`
    (registry.read_metrics_map) or, absent, this module's default. A count with
    no entry in the map is read and reported but written nowhere.

    `missing` — {read_key: text}: what to write when the picture shows NO such
    count (the Kashi deck prints "hidden" in Post Reach). Only for a key the
    map sends somewhere, only into a cell still blank, and only when the
    picture was actually read (not when OCR is missing or failed — then we do
    not know, and a blank is the honest cell). This pass is the last reader to
    run, so by now every number that could be read has been."""
    rp = Path(results_path)
    results = json.loads(rp.read_text(encoding="utf-8"))
    smap = _as_map(sheet_map)
    targets_keys = [k for _, keys in smap for k in keys]
    miss = {k: str(v).strip() for k, v in (missing or {}).items()
            if str(v or "").strip() and any(k == src for src, _ in smap)}

    def wants(r) -> bool:
        """Only a captured row with at least one blank TARGET cell is worth ~2s
        of OCR — an X row the page reader already filled is not."""
        if r.get("status") != "ok" or not r.get("screenshot"):
            return False
        have = r.get("sheet_metrics") or {}
        return not only_blank or any(not str(have.get(k) or "").strip()
                                     for k in targets_keys)

    targets = [(r.get("screenshot") or "", r.get("post_link") or r.get("url") or "",
                r.get("platform") or "auto")
               for r in results if wants(r)]
    rows = read_many(targets, engine)
    by_shot = {r["screenshot"]: r for r in rows}
    filled = worded = 0
    for r in results:
        got = by_shot.get(str(r.get("screenshot") or ""))
        # "ok" = at least one number read; "no_numbers" = the picture was read
        # and shows none. Both are a real look at the picture.
        if not got or got["status"] not in ("ok", "no_numbers"):
            continue
        metrics = dict(r.get("sheet_metrics") or {})
        for src, keys in smap:
            if got.get(src) is None:
                continue
            for k in keys:
                if only_blank and str(metrics.get(k) or "").strip():
                    continue                    # typed by a human -> untouched
                metrics[k] = got["shown"][src]  # exactly as the picture shows it
                filled += 1
        for src, keys in smap:
            text = miss.get(src)
            if not text or got.get(src) is not None:
                continue                        # shown, or no word asked for
            for k in keys:
                if str(metrics.get(k) or "").strip():
                    continue                    # typed, or just filled
                metrics[k] = text               # the post shows no such count
                worded += 1
            got.setdefault("evidence", []).append(
                f"{src} not shown -> {'/'.join(keys)} {text!r}")
        if metrics:
            r["sheet_metrics"] = metrics
    rp.write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_json(rows, Path(json_out) if json_out else rp.with_name("metrics_read.json"))
    write_csv(rows, Path(csv_out) if csv_out else rp.with_name("metrics_read.csv"))
    unread = sum(1 for r in rows if r["status"] != "ok")
    if worded:
        print(f"[shots] {worded} cell(s) marked as not shown by the post "
              f"({', '.join(sorted(set(miss.values())))})", flush=True)
    _say("shots_filled", filled, len(rows) - unread, len(rows))
    return filled


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
_CSV_COLUMNS = ("link", "screenshot", "platform", "status", "engine",
                "likes", "comments", "shares", "views", "followers", "bookmarks")


def write_json(rows: list, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[shots] wrote {dest}", flush=True)


def write_csv(rows: list, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c.title() for c in _CSV_COLUMNS] +
                   [f"{k.title()} (shown)" for k in KEYS] + ["Evidence"])
        for r in rows:
            w.writerow([r.get(c) if r.get(c) is not None else "" for c in _CSV_COLUMNS]
                       + [r["shown"][k] if r["shown"][k] != MISSING else "" for k in KEYS]
                       + [" | ".join(r.get("evidence") or [])])
    print(f"[shots] wrote {dest}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read likes / comments / shares / views off screenshots.")
    ap.add_argument("input", nargs="+",
                    help="screenshot(s), a folder of them, or a job's results.json")
    ap.add_argument("--platform", default="auto",
                    choices=("auto", "x", "facebook", "instagram"),
                    help="what the screenshots are of (default: from the link, "
                         "else auto)")
    ap.add_argument("--engine", default=os.environ.get("SHOT_METRICS_ENGINE", "tesseract"),
                    choices=("tesseract", "grok"),
                    help="tesseract (default, offline) or grok (needs XAI_API_KEY)")
    ap.add_argument("--fill", action="store_true",
                    help="with a results.json: fill its blank sheet_metrics in place")
    ap.add_argument("--map", dest="sheet_map", default="",
                    help="with --fill: where each count goes, e.g. "
                         "'likes=like;views=reach' (default: likes=like;"
                         "comments=comments;shares=shares;views=views,reach,impressions)")
    ap.add_argument("--missing", dest="missing", default="",
                    help="with --fill: text for a count the picture does not "
                         "show, e.g. 'views=hidden' (default: leave blank)")
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--csv", dest="csv_out", default="")
    args = ap.parse_args()

    if args.fill:
        rj = [i for i in args.input if i.endswith(".json")]
        if len(rj) != 1:
            print("[shots] --fill needs exactly one results.json", flush=True)
            sys.exit(2)
        try:
            smap = parse_map(args.sheet_map) if args.sheet_map else None
        except ValueError as e:
            print(f"[shots] {e}", flush=True)
            sys.exit(2)
        miss = {}
        for part in re.split(r"[;\s]+", args.missing.strip()):
            if part and "=" in part:
                k, v = part.split("=", 1)
                miss[k.strip().lower()] = v.strip()
        n = fill_results(rj[0], args.engine, json_out=args.json_out or None,
                         csv_out=args.csv_out or None, sheet_map=smap,
                         missing=miss)
        print(f"[shots] filled {n} sheet cell(s)", flush=True)
        return

    rows = read_many(targets_from(args.input, args.platform), args.engine)
    if not rows:
        sys.exit(1)
    for r in rows:
        s = r["shown"]
        print(f"  {r['status']:<11} likes {s['likes']:>7}  comments {s['comments']:>7}  "
              f"shares {s['shares']:>7}  views {s['views']:>7}  "
              f"{Path(r['screenshot']).name}")
    if args.json_out:
        write_json(rows, Path(args.json_out))
    if args.csv_out:
        write_csv(rows, Path(args.csv_out))


if __name__ == "__main__":
    main()

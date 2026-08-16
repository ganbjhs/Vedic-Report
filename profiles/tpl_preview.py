"""Two PIL pictures of a designed page: the Canva slot guide, and the page as
it will actually print.

  * `guide_png` — a TRANSPARENT layer at the page's real pixel size with every
    slot drawn as a labelled outline. The designer imports it into Canva as the
    top layer, draws the art underneath, deletes the layer, exports a PNG and
    drops that back into the app.
  * `page_png` — ONE finished page with sample data, drawn the way
    `tpl_builder.build_pdf` draws it, so a designer sees the real result while
    dragging boxes instead of after a capture run.

Both read the same fractions the builder reads and nothing else — that is the
only thing that makes them worth looking at. A guide that disagreed with the
document would be worse than no guide.

Why PIL and not "render the PDF": this project has no rasteriser (no pdftoppm,
no PyMuPDF, and adding one for a preview is not a trade worth making). So this
is a second implementation of the same drawing rules. Every number it uses
comes from `registry` / `layout` / `tpl_builder`, and `test_builder.py` asserts
the two agree on where a slot lands and how big its type is.

Pure presentation, zero captures: nothing here opens a browser or reads a
result dict it was not handed.
"""
import datetime
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import layout        # noqa: E402
import registry      # noqa: E402
import tpl_builder   # noqa: E402

PAPER_DPI = 150          # A4 / Letter: 1240x1754 and 1275x1650 — print-sane
SLIDE_DPI = 144          # 16:9 -> 1920x1080, 4:3 -> 1440x1080 exactly
PT = 72.0

FIXTURE = HERE.parent / "data" / "sample-fixture" / "screenshots"
LOGOS = HERE / "assets" / "logos"

# Colours of the guide layer, one per kind of slot, chosen to stay legible over
# whatever art ends up underneath.
GUIDE = {
    "slot":    ((37, 99, 235), "SCREENSHOT"),
    "logo":    ((124, 58, 237), "PLATFORM LOGO"),
    "summary": ((22, 163, 74), "SUMMARY TABLE"),
    "text":    ((217, 119, 6), ""),
}

# The one post the preview shows. Real-looking values, so a designer can see at
# a glance whether a pill is wide enough for "63,900" — which is the whole
# point of previewing before a capture run.
SAMPLE = {
    "account_name": "Kashi Ke Wasi",
    "category": "3rd Party Posts",
    "platform": "x",
    "post_link": "https://x.com/kashikewasi/status/1899000000000000000",
    "sheet_metrics": {"like": "676", "impressions": "63,900", "views": "63,000",
                      "reach": "41,200", "comments": "38", "shares": "12",
                      "followers": "18,400"},
    "post_no": 1,
    "post_total": 9,
    "title": "Sample Report",
    "sections": [("3rd Party Posts", 9), ("Own Posts", 6)],
}

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)
_font_cache = {}


def _font(size_px, bold=False, path=None):
    """A TTF at `size_px`. Helvetica is reportlab's built-in and has no file, so
    the preview substitutes the closest grotesque the machine has; the PDF is
    still the authority on metrics (that difference is documented in §18a)."""
    from PIL import ImageFont
    key = (round(size_px, 1), bool(bold), str(path or ""))
    if key in _font_cache:
        return _font_cache[key]
    tried = ([str(path)] if path else []) + list(
        _BOLD_CANDIDATES if bold else _FONT_CANDIDATES)
    if bold:
        tried += list(_FONT_CANDIDATES)          # bold file missing: use regular
    font = None
    for cand in tried:
        try:
            font = ImageFont.truetype(cand, max(1, int(round(size_px))))
            break
        except (OSError, ValueError):
            continue
    if font is None:                              # last resort, still draws
        try:
            font = ImageFont.load_default(max(1, int(round(size_px))))
        except TypeError:                         # Pillow < 10.1
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def page_pixels(profile) -> tuple:
    """(width_px, height_px, px_per_point) for this profile's page.

    Slides are rendered at the size Canva exports them (16:9 -> 1920x1080),
    paper at 150 dpi. Orientation is honoured because `registry.page_inches`
    honours it — this function never swaps anything itself.
    """
    page = profile["page"]
    size = str(page.get("size", "letter")).lower()
    dpi = SLIDE_DPI if size in ("16:9", "4:3") else PAPER_DPI
    w_in, h_in = registry.page_inches(page)
    return round(w_in * dpi), round(h_in * dpi), dpi / PT


def _items(profile, kind):
    """Everything drawn on one page kind: (what, box_fractions, text_item)."""
    tpl = profile["template"]
    out = []
    if kind == "post":
        for sl in tpl.get("slots") or []:
            out.append(("slot", sl, None))
        for lg in tpl.get("logos") or []:
            out.append(("logo", lg, None))
    if kind == "summary" and tpl.get("summary_box"):
        out.append(("summary", tpl["summary_box"], None))
    for t in tpl.get("text") or []:
        if t.get("page", "post") in (kind, "all"):
            out.append(("text", t, t))
    return out


# --------------------------------------------------------------------------- #
# Text — the same rules as tpl_builder.draw_text, in PIL
# --------------------------------------------------------------------------- #
def _text_font(profile, t, size_px, fonts=None):
    name = t.get("font") or registry.DEFAULT_FONT
    path = (fonts or {}).get(name) or registry.font_path(profile, name)
    return _font(size_px, bool(t.get("bold")), path)


def _trim(draw, value, font, max_w):
    """Same shrink-to-the-slot rule the PDF uses: drop two characters, add an
    ellipsis, try again."""
    while len(value) > 1 and draw.textlength(value, font=font) > max_w:
        value = value[:-2] + "…"
    return value


def _draw_text(draw, profile, t, value, W, H, ppp, fonts=None):
    if not value:
        return
    size_px = float(t.get("size_pt", 10)) * ppp
    font = _text_font(profile, t, size_px, fonts)
    x, w = t["x"] * W, t["w"] * W
    # reportlab puts the baseline `size` below the slot's top edge
    # (`y = H - t.y*H - size`); this is that same line in top-left coordinates.
    baseline = t["y"] * H + size_px
    value = _trim(draw, str(value), font, w)
    align = t.get("align", "left")
    anchor = {"center": "ms", "right": "rs"}.get(align, "ls")
    px = x + w / 2 if align == "center" else (x + w if align == "right" else x)
    draw.text((px, baseline), value, font=font,
              fill=t.get("color") or "#111111", anchor=anchor)


# --------------------------------------------------------------------------- #
# The Canva guide
# --------------------------------------------------------------------------- #
def _dashed_rect(draw, box, color, width, dash=18):
    x0, y0, x1, y1 = box
    for x in range(int(x0), int(x1), dash * 2):
        draw.rectangle([x, y0, min(x + dash, x1), y0 + width - 1], fill=color)
        draw.rectangle([x, y1 - width + 1, min(x + dash, x1), y1], fill=color)
    for y in range(int(y0), int(y1), dash * 2):
        draw.rectangle([x0, y, x0 + width - 1, min(y + dash, y1)], fill=color)
        draw.rectangle([x1 - width + 1, y, x1, min(y + dash, y1)], fill=color)


def _chip(draw, x, y, text, color, size_px):
    """A solid label above (or inside) a box, so the name is readable over any
    art the designer puts underneath."""
    font = _font(size_px, True)
    pad = max(3, round(size_px * 0.35))
    w = draw.textlength(text, font=font) + 2 * pad
    h = size_px + 2 * pad * 0.6
    y = max(0, y - h - 2)
    draw.rectangle([x, y, x + w, y + h], fill=color + (235,))
    draw.text((x + pad, y + h / 2), text, font=font, fill=(255, 255, 255, 255),
              anchor="lm")
    return h


def guide_png(profile, kind="post") -> bytes:
    """A transparent PNG the size of the page, with every slot outlined and
    named. Import it into Canva on top, design underneath, delete it, export."""
    from PIL import Image, ImageDraw

    W, H, ppp = page_pixels(profile)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    unit = max(2, round(min(W, H) / 380))          # line weight, page-relative
    label_px = max(13, round(min(W, H) / 62))

    n_slot = 0
    for what, box, t in _items(profile, kind):
        color, name = GUIDE[what]
        x0, y0 = box["x"] * W, box["y"] * H
        x1, y1 = x0 + box["w"] * W, y0 + box["h"] * H
        if what == "text":
            label = tpl_builder.FIELD_LABELS.get(t["field"], t["field"])
            size_px = float(t.get("size_pt", 10)) * ppp
            _dashed_rect(d, (x0, y0, x1, y1), color + (215,), max(1, unit // 2), 10)
            # Draw the field name AT ITS REAL SIZE inside the box: the designer
            # is choosing how much room the value needs, and only real type
            # answers that.
            ghost = dict(t, color=f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}")
            _draw_text(d, profile, ghost, label, W, H, ppp)
            # If the name does not fit the box at its real size, the outline
            # alone would not say WHICH field it is — chip it above.
            fits = d.textlength(label, font=_text_font(profile, t, size_px)) <= (x1 - x0)
            if size_px < label_px or not fits:
                _chip(d, x0, y0, label, color, label_px * 0.8)
            continue
        if what == "slot":
            n_slot += 1
            name = f"SCREENSHOT {n_slot}" if len(profile["template"].get("slots") or []) > 1 else "SCREENSHOT"
        _dashed_rect(d, (x0, y0, x1, y1), color + (255,), unit)
        _chip(d, x0, y0 + (0 if y0 > label_px * 2 else -label_px * 2.4), name,
              color, label_px)

    note = (f"SLOT GUIDE · {profile.get('label') or profile['slug']} · "
            f"{kind} page · {W}x{H}px — design UNDER this layer, then DELETE it")
    f = _font(label_px * 0.85, True)
    pad = label_px * 0.5
    w = d.textlength(note, font=f) + 2 * pad
    d.rectangle([0, 0, min(W, w), label_px * 2], fill=(15, 23, 42, 225))
    d.text((pad, label_px), note, font=f, fill=(255, 255, 255, 255), anchor="lm")

    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# The live preview
# --------------------------------------------------------------------------- #
def sample_shot():
    """A real captured post from the stored fixture, or a drawn stand-in.

    Depending on the fixture would make the designer break on a fresh checkout,
    so its absence is a fallback, not an error.
    """
    from PIL import Image
    if FIXTURE.is_dir():
        for p in sorted(FIXTURE.glob("*.png")):
            try:
                im = Image.open(p)
                im.load()
            except Exception:
                continue
            if im.height >= 200:
                return im.convert("RGB")
    import thumbnails
    return thumbnails._placeholder_post(598, 820)


def _sample_result():
    return {"status": "ok", "url": SAMPLE["post_link"],
            "post_link": SAMPLE["post_link"], "platform": SAMPLE["platform"],
            "account_name": SAMPLE["account_name"], "category": SAMPLE["category"],
            "sheet_metrics": dict(SAMPLE["sheet_metrics"])}


def _background(profile, kind, assets=None):
    """The designed page image: an unsaved upload wins, then the style's own
    asset, then the post page (the builder's own fallback)."""
    over = (assets or {}).get(kind)
    if over and Path(over).exists():
        return Path(over)
    for k in (kind, "post"):
        p = registry.asset_path(profile, k)
        if p and Path(p).exists():
            return Path(p)
        over = (assets or {}).get(k)
        if over and Path(over).exists():
            return Path(over)
    return None


def _summary_table(d, profile, box, W, H, ppp, rows):
    """The section -> count table, same arithmetic as tpl_builder.build_pdf."""
    bx, by = box["x"] * W, box["y"] * H
    bw, bh = box["w"] * W, box["h"] * H
    rh = min(bh / max(1, len(rows)), 0.42 * 72 * ppp)
    fs = max(8 * ppp, min(16 * ppp, rh * 0.5))
    for i, (name, n) in enumerate(rows):
        yy = by + i * rh
        last = i == len(rows) - 1
        d.rectangle([bx, yy, bx + bw, yy + rh],
                    fill="#FBEFE0" if i % 2 == 0 else "#FFFFFF",
                    outline="#8B5E34", width=max(1, round(0.6 * ppp)))
        d.line([bx + bw * 0.62, yy, bx + bw * 0.62, yy + rh], fill="#8B5E34",
               width=max(1, round(0.6 * ppp)))
        base = yy + rh - rh * 0.32
        d.text((bx + 8 * ppp, base), str(name)[:60], font=_font(fs, True),
               fill="#7A3E12", anchor="ls")
        d.text((bx + bw - 10 * ppp, base), str(n), font=_font(fs, last),
               fill="#111111", anchor="rs")


def page_png(profile, kind="post", assets=None, fonts=None, shot=None) -> bytes:
    """One page rendered with sample data — background, screenshots in their
    slots, logo, text slots, summary table."""
    from PIL import Image, ImageDraw

    W, H, ppp = page_pixels(profile)
    page = Image.new("RGB", (W, H), "#FFFFFF")
    bg = _background(profile, kind, assets)
    if bg:
        with Image.open(bg) as b:
            page.paste(b.convert("RGB").resize((W, H), Image.LANCZOS))
    d = ImageDraw.Draw(page)

    tpl = profile["template"]
    r = _sample_result()
    n_slots = max(1, len(tpl.get("slots") or []))
    base_ctx = {"title": SAMPLE["title"],
                "date": datetime.date.today().strftime("%d-%m-%Y"), "pages": 3}

    if kind == "post":
        master = shot or sample_shot()
        # The real builder composites per the image spec, then places by
        # `layout.placements` — identical call here, so the picture in the slot
        # is the size the PDF will use.
        import shapes
        provisional = layout.placements(profile, [master.size] * n_slots)
        composed, dims = [], []
        for prov in provisional:
            im = shapes.compose(master, profile["image"], placement_w_in=prov.w_in)
            composed.append(im.convert("RGB"))
            dims.append(im.size)
        for place, im in zip(layout.placements(profile, dims), composed):
            pw_in, _ = registry.page_inches(profile["page"])
            scale = W / pw_in
            box = (max(1, round(place.w_in * scale)), max(1, round(place.h_in * scale)))
            page.paste(im.resize(box, Image.LANCZOS),
                       (round(place.x_in * scale), round(place.y_in * scale)))
        logo = LOGOS / f"{SAMPLE['platform']}.png"
        for lg in tpl.get("logos") or []:
            if not logo.exists():
                continue
            with Image.open(logo) as lo:
                lo = lo.convert("RGBA")
                bw, bh = lg["w"] * W, lg["h"] * H
                s = min(bw / lo.width, bh / lo.height)
                lo = lo.resize((max(1, round(lo.width * s)), max(1, round(lo.height * s))),
                               Image.LANCZOS)
                page.paste(lo, (round(lg["x"] * W + (bw - lo.width) / 2),
                                round(lg["y"] * H + (bh - lo.height) / 2)), lo)
        ctx = tpl_builder._post_ctx(base_ctx, r, 1, SAMPLE["post_no"],
                                    SAMPLE["post_total"])
    else:
        ctx = {**base_ctx, "page": 0, "post_link": "", "metrics_dict": {}}
        if kind == "summary":
            box = tpl.get("summary_box") or {"x": 0.36, "y": 0.45, "w": 0.6, "h": 0.5}
            rows = SAMPLE["sections"] + [("Total Items Tracked", 15)]
            _summary_table(d, profile, box, W, H, ppp, rows)

    for t in tpl.get("text") or []:
        if t.get("page", "post") not in (kind, "all"):
            continue
        _draw_text(d, profile, t, tpl_builder._field_value(t["field"], ctx),
                   W, H, ppp, fonts)

    buf = io.BytesIO()
    page.save(buf, "PNG", optimize=True)
    return buf.getvalue()

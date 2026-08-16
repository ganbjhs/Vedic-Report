"""Documents for TEMPLATE styles — pages designed in Canva (or anything that
exports PNG) with screenshot / text slots drawn on top in the app.

Same inputs as prof_builder (`results`, prepared JPEGs, `layout.placements`),
different drawing: every page starts as the designed image scaled to the paper,
screenshots are fitted (never cropped) into the slots in reading order, and
text slots print report fields. Nothing here reads a screenshot's pixels
beyond scaling — the capture is untouched, this is presentation only.

Fidelity by output:
  * PDF  — exact: background image, screenshots and text at their slots.
  * HTML — exact: absolutely-positioned layers, everything inlined.
  * DOCX — approximate BY DESIGN: Word cannot reliably layer a picture over a
           full-page background, so each page is the designed image followed by
           its screenshots and captions. It is labelled as such in the file.
"""
import base64
import datetime
import html
from pathlib import Path

import registry
import layout


def _text_items(profile, page_kind):
    for t in (profile["template"].get("text") or []):
        pg = t.get("page", "post")
        if pg == page_kind or pg == "all":
            yield t


def _field_value(field, ctx):
    v = ctx.get(field, "")
    if field == "metrics":
        m = ctx.get("metrics_dict") or {}
        v = " · ".join(f"{k.title()}: {m.get(k, '—')}" for k in
                       ("followers", "reactions", "comments", "reach", "shares") if k in m) or "—"
    return "" if v is None else str(v)


def _pages(results, places):
    """[(page_index, [(result, image, place), ...]), ...] in order."""
    out, cur, bucket = [], -1, []
    for r, img, pl in zip(results, places[0], places[1]):
        if pl.page != cur:
            if bucket:
                out.append((cur, bucket))
            cur, bucket = pl.page, []
        bucket.append((r, img, pl))
    if bucket:
        out.append((cur, bucket))
    return out


def _bg(profile, kind):
    p = registry.asset_path(profile, kind)
    return str(p) if p and Path(p).exists() else None


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def build_pdf(results, images, places, profile, title, out):
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as pdfcanvas

    pw_in, ph_in = registry.page_inches(profile["page"])
    W, H = pw_in * inch, ph_in * inch
    c = pdfcanvas.Canvas(str(out), pagesize=(W, H))
    c.setTitle(title)
    date = datetime.date.today().strftime("%d-%m-%Y")
    pages = _pages(results, (images, places))
    n_pages = len(pages)

    def paint_bg(kind):
        bg = _bg(profile, kind) or _bg(profile, "post")
        if bg:
            c.drawImage(bg, 0, 0, width=W, height=H, mask="auto")

    def draw_text(t, ctx):
        value = _field_value(t["field"], ctx)
        if not value:
            return
        size = float(t.get("size_pt", 10))
        c.setFont("Helvetica-Bold" if t.get("bold") else "Helvetica", size)
        c.setFillColor(colors.HexColor(t.get("color") or "#111111"))
        x, y, w = t["x"] * W, H - t["y"] * H - size, t["w"] * W
        # trim to the slot width
        while len(value) > 1 and c.stringWidth(value, "Helvetica", size) > w:
            value = value[:-2] + "…"
        if t.get("align") == "center":
            c.drawCentredString(x + w / 2, y, value)
        elif t.get("align") == "right":
            c.drawRightString(x + w, y, value)
        else:
            c.drawString(x, y, value)
        if t["field"] == "post_link" and ctx.get("post_link"):
            c.linkURL(ctx["post_link"], (x, y - 2, x + w, y + size), relative=0)

    base_ctx = {"title": title, "date": date, "pages": n_pages}

    if _bg(profile, "cover"):
        paint_bg("cover")
        for t in _text_items(profile, "cover"):
            draw_text(t, {**base_ctx, "page": 0})
        c.showPage()

    for page_no, (pidx, items) in enumerate(pages, start=1):
        paint_bg("post")
        for r, img, pl in items:
            x = pl.x_in * inch
            y = H - (pl.y_in + pl.h_in) * inch
            c.drawImage(img, x, y, width=pl.w_in * inch, height=pl.h_in * inch,
                        preserveAspectRatio=True, anchor="c", mask="auto")
        # per-post text slots take the FIRST post on the page's fields (single-
        # slot templates, the common case); page-level fields work everywhere.
        r0 = items[0][0]
        ctx = {**base_ctx, "page": page_no, "index": r0.get("index", ""),
               "account_name": r0.get("account_name", ""),
               "post_link": (r0.get("post_link") or r0.get("url") or ""),
               "category": r0.get("category", ""), "metrics_dict": r0.get("metrics")}
        for t in _text_items(profile, "post"):
            draw_text(t, ctx)
        c.showPage()

    if _bg(profile, "end") or profile["content"].get("links_table"):
        if _bg(profile, "end"):
            paint_bg("end")
            for t in _text_items(profile, "end"):
                draw_text(t, {**base_ctx, "page": n_pages})
        if profile["content"].get("links_table"):
            y = H * 0.82
            c.setFont("Helvetica-Bold", 14)
            c.setFillColor(colors.HexColor("#0F172A"))
            c.drawString(0.75 * inch, y, "Links")
            y -= 18
            c.setFont("Helvetica", 8)
            c.setFillColor(colors.HexColor("#1D4ED8"))
            for r in results:
                link = (r.get("post_link") or r.get("url") or "").strip()
                if not link:
                    continue
                if y < 0.75 * inch:
                    c.showPage()
                    y = H - 0.75 * inch
                    c.setFont("Helvetica", 8)
                    c.setFillColor(colors.HexColor("#1D4ED8"))
                c.drawString(0.75 * inch, y, link[:150])
                c.linkURL(link, (0.75 * inch, y - 2, W - 0.75 * inch, y + 8), relative=0)
                y -= 12
        c.showPage()
    c.save()


# --------------------------------------------------------------------------- #
# HTML — one file, everything inlined, pages absolutely positioned
# --------------------------------------------------------------------------- #
def _data_uri(path):
    p = Path(path)
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def build_html(results, images, places, profile, title, out):
    pw_in, ph_in = registry.page_inches(profile["page"])
    date = datetime.date.today().strftime("%d-%m-%Y")
    pages = _pages(results, (images, places))
    n_pages = len(pages)
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>',
             '<style>body{margin:0;background:#e5e7eb;font-family:Helvetica,Arial,sans-serif}'
             f'.pg{{position:relative;width:{pw_in}in;height:{ph_in}in;margin:16px auto;background:#fff;'
             'box-shadow:0 2px 12px rgba(0,0,0,.15);overflow:hidden;page-break-after:always;background-size:100% 100%}'
             '.pg img.shot{position:absolute;object-fit:contain}'
             '.pg .t{position:absolute;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.1}'
             '@media print{body{background:#fff}.pg{margin:0;box-shadow:none}}</style></head><body>']

    def bg_style(kind):
        bg = _bg(profile, kind) or _bg(profile, "post")
        return f"background-image:url('{_data_uri(bg)}')" if bg else ""

    def text_html(t, ctx):
        v = _field_value(t["field"], ctx)
        if not v:
            return ""
        style = (f"left:{t['x']*100:.3f}%;top:{t['y']*100:.3f}%;width:{t['w']*100:.3f}%;"
                 f"font-size:{t.get('size_pt', 10)}pt;color:{t.get('color') or '#111'};"
                 f"text-align:{t.get('align', 'left')};font-weight:{'700' if t.get('bold') else '400'}")
        inner = html.escape(v)
        if t["field"] == "post_link" and ctx.get("post_link"):
            inner = f'<a href="{html.escape(ctx["post_link"], quote=True)}" style="color:inherit">{inner}</a>'
        return f'<div class="t" style="{style}">{inner}</div>'

    base_ctx = {"title": title, "date": date, "pages": n_pages}
    if _bg(profile, "cover"):
        parts.append(f'<div class="pg" style="{bg_style("cover")}">' +
                     "".join(text_html(t, {**base_ctx, "page": 0}) for t in _text_items(profile, "cover")) + "</div>")
    for page_no, (pidx, items) in enumerate(pages, start=1):
        parts.append(f'<div class="pg" style="{bg_style("post")}">')
        for r, img, pl in items:
            parts.append(f'<img class="shot" src="{_data_uri(img)}" alt="" style="left:{pl.x_in}in;top:{pl.y_in}in;width:{pl.w_in}in;height:{pl.h_in}in">')
        r0 = items[0][0]
        ctx = {**base_ctx, "page": page_no, "account_name": r0.get("account_name", ""),
               "post_link": (r0.get("post_link") or r0.get("url") or ""),
               "category": r0.get("category", ""), "metrics_dict": r0.get("metrics")}
        parts.append("".join(text_html(t, ctx) for t in _text_items(profile, "post")))
        parts.append("</div>")
    if profile["content"].get("links_table"):
        parts.append(f'<div class="pg" style="{bg_style("end") if _bg(profile, "end") else ""}"><div style="position:absolute;left:0.75in;top:1in;right:0.75in;font-size:8pt">'
                     '<h2 style="margin:0 0 8px;font-size:14pt">Links</h2>' +
                     "".join(f'<div><a href="{html.escape(l, quote=True)}">{html.escape(l)}</a></div>'
                             for l in ((r.get("post_link") or r.get("url") or "") for r in results) if l) +
                     "</div></div>")
    parts.append("</body></html>")
    Path(out).write_text("".join(parts), encoding="utf-8")


# --------------------------------------------------------------------------- #
# DOCX — approximate (see module docstring)
# --------------------------------------------------------------------------- #
def build_docx(results, images, places, profile, title, out):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    pw_in, ph_in = registry.page_inches(profile["page"])
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(pw_in), Inches(ph_in)
    m = 0.5
    sec.top_margin = sec.bottom_margin = sec.left_margin = sec.right_margin = Inches(m)
    usable_w = pw_in - 2 * m
    pages = _pages(results, (images, places))

    def add_bg(kind, height_in):
        bg = _bg(profile, kind) or _bg(profile, "post")
        if bg:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(bg, width=Inches(usable_w),
                                    height=Inches(usable_w * ph_in / pw_in * height_in))

    if _bg(profile, "cover"):
        add_bg("cover", 1.0)
        doc.add_page_break()
    for pidx, items in pages:
        add_bg("post", 0.42)          # the designed page as a header strip
        for r, img, pl in items:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            w = min(pl.w_in * 1.15, usable_w)
            p.add_run().add_picture(img, width=Inches(w))
            cap = doc.add_paragraph()
            run = cap.add_run((r.get("account_name") or "") + "  " +
                              (r.get("post_link") or r.get("url") or ""))
            run.font.size = Pt(8)
            run.font.color.rgb = RGBColor(0x1D, 0x4E, 0xD8)
        doc.add_page_break()
    if profile["content"].get("links_table"):
        doc.add_heading("Links", level=1)
        for r in results:
            link = (r.get("post_link") or r.get("url") or "")
            if link:
                doc.add_paragraph(link)
    note = doc.add_paragraph()
    nr = note.add_run("This Word version is an approximation of the designed template — "
                      "the PDF is the faithful rendering.")
    nr.font.size = Pt(7)
    nr.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)
    doc.save(str(out))


BUILDERS = {"pdf": build_pdf, "docx": build_docx, "html": build_html}

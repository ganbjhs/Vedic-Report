"""Build a profile's documents from results.json — PDF, DOCX and/or HTML.

Geometry comes from `layout.py`, shape from `shapes.py`; this module only draws.
That split is what makes the whole layout testable with no browser and no
rasteriser (`tests/test_parity.py`).

`src/report_builder.py` and `influencer/inf_report_builder.py` are NOT imported
and NOT modified. The two existing report types keep their own builders and
entrypoints (docs/profile-engine.md §10 decision 3); this one serves additional
profiles.

Usage: python profiles/prof_builder.py "<header>" "<stem>" "<profile-slug>"
"""
import base64
import html
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[0].parent if HERE.name != "profiles" else HERE.parent
sys.path.insert(0, str(HERE))
import layout        # noqa: E402
import progress      # noqa: E402
import registry      # noqa: E402
import shapes        # noqa: E402

OUT = ROOT / "reports"

_JPEG_QUALITY = 88          # matches the frozen builders — no visible loss
INCH = 72.0


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def usable(r) -> bool:
    """Only a cleanly captured link belongs in a document.

    Identical rule to the frozen builders: status must be exactly "ok", so every
    demoted status (overlay_blocked, parent_lost, age_restricted, ...) is left
    out with no per-status logic here.
    """
    if r.get("status") != "ok":
        return False
    shot = r.get("screenshot")
    return bool(shot) and Path(shot).exists() and Path(shot).stat().st_size > 0


def _resolve(path_str):
    """results.json holds ABSOLUTE paths, which is correct and load-bearing
    (RULEBOOK rule 2 — the code is copied into the job dir, so they resolve
    inside it). A consumer running here, inside the job, may trust them; we only
    fall back to the local screenshots folder if the file has moved."""
    p = Path(path_str)
    if p.exists():
        return p
    local = OUT / "screenshots" / p.name
    return local if local.exists() else p


def prepare(results, profile, workdir):
    """Composite each master per the profile, then hand back placements.

    Two-pass, as docs/profile-engine.md requires: `bordered`/`shadowed` grow the
    image and therefore change its aspect, so the placement is computed from the
    master, used to scale point-valued decoration, and then recomputed from the
    composed image.
    """
    from PIL import Image

    spec = profile["image"]
    masters = []
    for r in results:
        with Image.open(_resolve(r["screenshot"])) as im:
            masters.append(im.convert("RGB"))

    # pass 1 — provisional placement, only to learn each image's width in inches
    provisional = layout.placements(profile, [m.size for m in masters])

    prepared, dims = [], []
    for i, (im, place) in enumerate(zip(masters, provisional), 1):
        composed = shapes.compose(im, spec, placement_w_in=place.w_in)
        dst = Path(workdir) / f"{i:03d}.jpg"
        composed.convert("RGB").save(dst, "JPEG", quality=_JPEG_QUALITY,
                                     optimize=True, subsampling=0)
        prepared.append(str(dst))
        dims.append(composed.size)
        if composed is not im:
            composed.close()
        im.close()

    # pass 2 — the placement the document actually uses
    return prepared, layout.placements(profile, dims)


def shown_link(r) -> str:
    link = (r.get("post_link") or r.get("url") or "").strip()
    return "" if link.startswith("file://") else link


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def build_pdf(results, images, places, profile, title, out):
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as pdfcanvas

    pw_in, ph_in = registry.page_inches(profile["page"])
    page_w, page_h = pw_in * inch, ph_in * inch
    top, right, bottom, left = profile["page"]["margins_in"]
    content = profile["content"]
    accent = colors.HexColor("#1D9BF0")

    c = pdfcanvas.Canvas(str(out), pagesize=(page_w, page_h))
    c.setTitle(title)

    def footer(page_no, pages):
        if not content.get("footer"):
            return
        text = content["footer"].format(page=page_no, pages=pages,
                                        title=title)
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#64748B"))
        c.drawCentredString(page_w / 2, bottom * inch / 2, text)

    n_pages = (max((p.page for p in places), default=-1) + 1) if places else 0

    if content.get("cover"):
        c.setFont("Helvetica-Bold", 26)
        c.setFillColor(colors.HexColor("#0F172A"))
        c.drawCentredString(page_w / 2, page_h * 0.56, title)
        c.setStrokeColor(accent)
        c.setLineWidth(2)
        c.line(page_w * 0.3, page_h * 0.53, page_w * 0.7, page_h * 0.53)
        c.setFont("Helvetica", 11)
        c.setFillColor(colors.HexColor("#64748B"))
        c.drawCentredString(page_w / 2, page_h * 0.49,
                            f"{len(results)} post(s)")
        c.showPage()

    current = -1
    cursor_y = None          # lowest ink on the current page, for the links table
    for r, img, place in zip(results, images, places):
        if place.page != current:
            if current >= 0:
                footer(current + 1, n_pages)
                c.showPage()
            current = place.page
            cursor_y = None
            if content.get("header"):
                c.setFont("Helvetica-Bold", 13)
                c.setFillColor(colors.HexColor("#0F172A"))
                c.drawCentredString(page_w / 2,
                                    page_h - top * inch * 0.55,
                                    content["header"].format(title=title))

        # reportlab's origin is bottom-left; layout works from the top.
        x = place.x_in * inch
        y = page_h - (place.y_in + place.h_in) * inch
        c.drawImage(img, x, y, width=place.w_in * inch, height=place.h_in * inch,
                    preserveAspectRatio=True, anchor="n", mask="auto")

        caption_y = y - 11
        for field in content.get("per_post_fields") or []:
            value = r.get(field) or ""
            if field == "post_link":
                value = shown_link(r)
            if not value:
                continue
            c.setFont("Helvetica", 7.5)
            c.setFillColor(accent if field == "post_link"
                           else colors.HexColor("#334155"))
            c.drawString(x, caption_y, str(value)[:110])
            if field == "post_link":
                c.linkURL(value, (x, caption_y - 2, x + place.w_in * inch,
                                  caption_y + 8), relative=0)
            caption_y -= 10

        for label, key in content.get("metrics") or []:
            val = (r.get("metrics") or {}).get(key, "—")
            c.setFont("Helvetica", 7.5)
            c.setFillColor(colors.HexColor("#334155"))
            c.drawString(x, caption_y, f"{label}: {val}")
            caption_y -= 9.5

        cursor_y = caption_y if cursor_y is None else min(cursor_y, caption_y)

    if places:
        footer(current + 1, n_pages)

    # The links table FLOWS below the last screenshot rather than starting its
    # own page — matching the frozen builders, which say so explicitly ("no page
    # break before it"). Forcing a page here made the twitter profile 9 pages
    # against the frozen builder's 8, which the geometry parity test could not
    # see because it only compares image placements.
    if content.get("links_table"):
        need = 3 * 12 + 26                     # heading + a few rows
        if cursor_y is None or cursor_y - 26 < bottom * inch + need:
            if places:
                c.showPage()
            cursor_y = page_h - top * inch + 14
        y = cursor_y - 26
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(colors.HexColor("#0F172A"))
        c.drawString(left * inch, y, "Links")
        y -= 18
        c.setFont("Helvetica", 8)
        for r in results:
            link = shown_link(r)
            if not link:
                continue
            if y < bottom * inch:
                c.showPage()
                y = page_h - top * inch
                c.setFont("Helvetica", 8)
            c.setFillColor(accent)
            c.drawString(left * inch, y, link[:150])
            c.linkURL(link, (left * inch, y - 2,
                             page_w - right * inch, y + 8), relative=0)
            y -= 12
        c.showPage()
    elif places:
        c.showPage()

    c.save()


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #
def build_docx(results, images, places, profile, title, out):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    content = profile["content"]
    pw_in, ph_in = registry.page_inches(profile["page"])
    top, right, bottom, left = profile["page"]["margins_in"]

    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(pw_in), Inches(ph_in)
    section.top_margin, section.bottom_margin = Inches(top), Inches(bottom)
    section.left_margin, section.right_margin = Inches(left), Inches(right)

    if content.get("header"):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(content["header"].format(title=title))
        run.bold = True
        run.font.size = Pt(16)

    per_page = registry.per_page(profile)
    for i, (r, img, place) in enumerate(zip(results, images, places)):
        if i and i % per_page == 0:
            doc.add_page_break()
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(img, width=Inches(place.w_in),
                                height=Inches(place.h_in))
        for field in content.get("per_post_fields") or []:
            value = shown_link(r) if field == "post_link" else (r.get(field) or "")
            if not value:
                continue
            cp = doc.add_paragraph()
            run = cp.add_run(str(value))
            run.font.size = Pt(8)
            if field == "post_link":
                run.font.color.rgb = RGBColor(0x1D, 0x9B, 0xF0)
        for label, key in content.get("metrics") or []:
            mp = doc.add_paragraph()
            mrun = mp.add_run(f"{label}: {(r.get('metrics') or {}).get(key, '—')}")
            mrun.font.size = Pt(8)

    if content.get("links_table"):
        doc.add_heading("Links", level=1)
        table = doc.add_table(rows=1, cols=1)
        table.style = "Table Grid"
        hdr = table.rows[0].cells[0].paragraphs[0].add_run("Link")
        hdr.bold = True
        hdr.font.color.rgb = RGBColor(0x1D, 0x9B, 0xF0)
        for r in results:
            link = shown_link(r)
            table.add_row().cells[0].text = link or "—"

    doc.save(str(out))


# --------------------------------------------------------------------------- #
# HTML — one self-contained file, images inlined
# --------------------------------------------------------------------------- #
def build_html(results, images, places, profile, title, out):
    content = profile["content"]
    cols = profile["page"]["grid"][0]
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>{html.escape(title)}</title>",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<style>",
        ":root{--ink:#0f172a;--muted:#64748b;--accent:#1d9bf0;--bg:#f6f8fa;",
        "--surface:#fff;--border:#e2e8f0}",
        "@media(prefers-color-scheme:dark){:root{--ink:#e6edf7;--muted:#93a3bb;",
        "--bg:#0b1220;--surface:#121a2a;--border:#24314a}}",
        "*{box-sizing:border-box}body{margin:0;background:var(--bg);",
        "color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,",
        "'Segoe UI',Roboto,sans-serif}",
        ".wrap{max-width:1100px;margin:0 auto;padding:32px 20px 64px}",
        "h1{font-size:1.6rem;margin:0 0 24px;text-align:center}",
        f".grid{{display:grid;gap:24px;grid-template-columns:repeat({cols},minmax(0,1fr))}}",
        "@media(max-width:720px){.grid{grid-template-columns:1fr}}",
        ".post{background:var(--surface);border:1px solid var(--border);",
        "border-radius:12px;padding:14px;overflow:hidden}",
        ".post img{width:100%;height:auto;display:block;border-radius:8px}",
        ".meta{font-size:.82rem;color:var(--muted);margin-top:8px;",
        "overflow-wrap:anywhere}",
        ".meta a{color:var(--accent)}",
        ".metrics{display:grid;grid-template-columns:max-content 1fr;",
        "gap:2px 12px;font-size:.82rem;margin-top:8px}",
        ".metrics dt{color:var(--muted)}.metrics dd{margin:0}",
        "table{width:100%;border-collapse:collapse;margin-top:32px;",
        "font-size:.85rem}td,th{border:1px solid var(--border);padding:6px 9px;",
        "text-align:left;overflow-wrap:anywhere}th{color:var(--accent)}",
        "</style></head><body><main class=\"wrap\">",
        f"<h1>{html.escape(title)}</h1><div class=\"grid\">",
    ]
    for r, img in zip(results, images):
        data = base64.b64encode(Path(img).read_bytes()).decode("ascii")
        parts.append("<article class=\"post\">")
        parts.append(f'<img alt="Post by {html.escape(r.get("account_name") or "")}" '
                     f'src="data:image/jpeg;base64,{data}">')
        for field in content.get("per_post_fields") or []:
            value = shown_link(r) if field == "post_link" else (r.get(field) or "")
            if not value:
                continue
            if field == "post_link":
                esc = html.escape(value, quote=True)
                parts.append(f'<p class="meta"><a href="{esc}">{html.escape(value)}</a></p>')
            else:
                parts.append(f'<p class="meta">{html.escape(str(value))}</p>')
        if content.get("metrics"):
            parts.append("<dl class=\"metrics\">")
            for label, key in content["metrics"]:
                val = (r.get("metrics") or {}).get(key, "—")
                parts.append(f"<dt>{html.escape(label)}</dt>"
                             f"<dd>{html.escape(str(val))}</dd>")
            parts.append("</dl>")
        parts.append("</article>")
    parts.append("</div>")

    if content.get("links_table"):
        parts.append("<table><thead><tr><th>Link</th></tr></thead><tbody>")
        for r in results:
            link = shown_link(r)
            if link:
                esc = html.escape(link, quote=True)
                parts.append(f'<tr><td><a href="{esc}">{html.escape(link)}</a></td></tr>')
        parts.append("</tbody></table>")

    parts.append("</main></body></html>")
    Path(out).write_text("".join(parts), encoding="utf-8")


BUILDERS = {"pdf": build_pdf, "docx": build_docx, "html": build_html}


def _mb(path):
    return round(Path(path).stat().st_size / 1_048_576, 1)


def main():
    title = sys.argv[1] if len(sys.argv) > 1 else "Report"
    stem = sys.argv[2] if len(sys.argv) > 2 else "Report"
    slug = sys.argv[3] if len(sys.argv) > 3 else "twitter"
    profile = registry.load(slug)

    all_results = json.loads((OUT / "results.json").read_text())
    results = [r for r in all_results if usable(r)]
    skipped = len(all_results) - len(results)
    if not results:
        print(f"[report] no capturable links ({skipped} skipped) — "
              "nothing to build", flush=True)
        return

    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        images, places = prepare(results, profile, td)
        for kind in profile["outputs"]:
            builder = BUILDERS.get(kind)
            if not builder:
                continue
            dest = OUT / f"{stem}.{kind}"
            builder(results, images, places, profile, title, dest)
            progress.wrote(dest, _mb(dest))
    extra = f"  ({skipped} skipped)" if skipped else ""
    print(f"[report] {len(results)} post(s) in {slug}{extra}", flush=True)


if __name__ == "__main__":
    main()

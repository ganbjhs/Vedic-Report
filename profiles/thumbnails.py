"""Render a mini page preview for each profile, from that profile's own config.

The dashboard used to describe each report type in a paragraph. A picture of the
actual page says it faster: 1-up letter, 2-up A4, a 2x3 contact sheet, a cover
followed by a padded card. Crucially the picture is DERIVED, not drawn by hand —
it runs the same `layout.placements()` the real builder uses and the same
`shapes.compose()` treatment, so a thumbnail cannot disagree with the document.

Placeholder posts rather than real screenshots: at ~150px per page a real
capture is illegible mush, and depending on a fixture would make the dashboard
fail when the fixture is absent. The placeholder keeps the recognisable shape of
a post (avatar, name line, text lines, media block) so the grid reads correctly,
and it still goes through `shapes.compose`, so rounded corners, borders, shadows
and aspect padding are the profile's real ones.

Filenames carry a hash of the profile, so an edited profile gets a new URL and
no stale image can be served from a cache.

No new dependency: Pillow is already used by the builders.
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import layout        # noqa: E402
import registry      # noqa: E402
import shapes        # noqa: E402

PAGE_W = 150             # px per rendered page; two pages when a cover exists
GUTTER = 8
_SUPERSAMPLE = 2         # render big, downscale once — cheap anti-aliasing

INK = (15, 23, 42)
MUTED = (148, 163, 184)
LINE = (203, 213, 225)
CARD = (255, 255, 255)
PAGE_EDGE = (203, 213, 225)
MEDIA = (226, 232, 240)
ACCENT = (29, 155, 240)


def _placeholder_post(width=598, height=760):
    """A grey stand-in with the silhouette of a post."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (width, height), CARD)
    d = ImageDraw.Draw(im)
    pad = 26
    d.ellipse([pad, pad, pad + 56, pad + 56], fill=LINE)              # avatar
    d.rounded_rectangle([pad + 72, pad + 8, pad + 300, pad + 26], 9, fill=LINE)
    d.rounded_rectangle([pad + 72, pad + 34, pad + 210, pad + 48], 7, fill=MEDIA)
    y = pad + 78
    for w in (width - 2 * pad, width - 2 * pad - 60, width - 2 * pad - 170):
        d.rounded_rectangle([pad, y, pad + w, y + 15], 7, fill=LINE)
        y += 26
    if height > y + 90:                                               # media block
        d.rounded_rectangle([pad, y + 12, width - pad, height - pad], 16, fill=MEDIA)
    return im


def _page(profile, n_posts, width, with_cover_slot=False):
    """One page of the profile's grid, at `width` px."""
    from PIL import Image, ImageDraw

    pw_in, ph_in = registry.page_inches(profile["page"])
    scale = width / pw_in
    height = round(ph_in * scale)
    page = Image.new("RGB", (width, height), CARD)
    ImageDraw.Draw(page).rectangle([0, 0, width - 1, height - 1], outline=PAGE_EDGE)

    if with_cover_slot:
        d = ImageDraw.Draw(page)
        d.rounded_rectangle([width * 0.22, height * 0.42,
                             width * 0.78, height * 0.46], 4, fill=INK)
        d.rectangle([width * 0.30, height * 0.50, width * 0.70,
                     height * 0.505], fill=ACCENT)
        d.rounded_rectangle([width * 0.36, height * 0.55,
                             width * 0.64, height * 0.575], 4, fill=MUTED)
        return page

    spec = profile["image"]
    dims, composed = [], []
    for i in range(n_posts):
        # Vary the heights a little so a grid does not look like a wallpaper.
        h = (760, 620, 900, 700, 820, 660)[i % 6]
        post = _placeholder_post(598, h)
        # placement_w_in only scales point-valued decoration; a provisional
        # value is fine here since the thumbnail is not a measuring device.
        out = shapes.compose(post, spec, placement_w_in=profile["image"]["max_in"][0])
        composed.append(out)
        dims.append(out.size)

    for place, im in zip(layout.placements(profile, dims), composed):
        if place.page > 0:
            break                       # this thumbnail shows one page only
        box = (max(1, round(place.w_in * scale)), max(1, round(place.h_in * scale)))
        page.paste(im.convert("RGB").resize(box, Image.LANCZOS),
                   (round(place.x_in * scale), round(place.y_in * scale)))
    return page


def render(profile, page_w=PAGE_W):
    """The finished thumbnail: cover + first page, or just the first page."""
    from PIL import Image

    w = page_w * _SUPERSAMPLE
    pages = []
    if profile["content"].get("cover"):
        pages.append(_page(profile, 0, w, with_cover_slot=True))
    pages.append(_page(profile, registry.per_page(profile), w))

    gap = GUTTER * _SUPERSAMPLE
    total_w = sum(p.width for p in pages) + gap * (len(pages) - 1)
    total_h = max(p.height for p in pages)
    strip = Image.new("RGB", (total_w, total_h), (241, 245, 249))
    x = 0
    for p in pages:
        strip.paste(p, (x, 0))
        x += p.width + gap
    return strip.resize((total_w // _SUPERSAMPLE, total_h // _SUPERSAMPLE),
                        Image.LANCZOS)


def profile_hash(profile) -> str:
    """Short digest of the resolved profile — the cache-busting half of the
    filename, so an edited profile can never serve a stale picture."""
    blob = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def generate(dest: Path, slugs=None) -> dict:
    """Render every profile into `dest`. Returns {slug: filename}.

    Regenerates only what changed (the hash is in the name) and deletes the
    stale files it replaces, so the directory cannot accumulate orphans or serve
    a picture of a profile that no longer looks like that.
    """
    dest.mkdir(parents=True, exist_ok=True)
    slugs = slugs if slugs is not None else registry.available()

    wanted, made = {}, {}
    for slug in slugs:
        try:
            profile = registry.load(slug)
            name = f"{slug}-{profile_hash(profile)}.png"
            wanted[slug] = name
            path = dest / name
            if not path.exists():
                render(profile).save(path, "PNG", optimize=True)
                made[slug] = name
        except Exception as e:                       # never break the dashboard
            print(f"[thumbnails] {slug}: {e}", flush=True)

    keep = set(wanted.values())
    for old in dest.glob("*.png"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    if made:
        print(f"[thumbnails] rendered {len(made)}: {', '.join(sorted(made))}",
              flush=True)
    return wanted


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        HERE.parent / "webapp" / "static" / "profiles")
    for slug, name in generate(out).items():
        print(f"  {slug:16} {name}")

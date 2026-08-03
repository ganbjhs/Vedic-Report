"""Compositing: turn a master screenshot into the shape a profile asks for.

Pure PIL, image in / image out, no file I/O and no globals — so every op is
unit-testable with zero captures.

WHAT THIS CAN AND CANNOT DO. The master PNG is *exactly* the tweet article's
bounding box (`x_capture._crop_box`), with no bleed. So nothing here can widen a
shot or recover a pixel that was never captured — every op either **adds**
around the master or **removes** from it. Shape is achieved by compositing onto
a canvas, never by cropping to fill.

UNITS. `radius_pt`, `border.pt`, `shadow.blur_pt` and `shadow.dy_pt` are in
POINTS AT PLACEMENT SIZE, not master pixels (docs/profile-engine.md §5.3). A
master is 598px wide at DPR 1 and 1196px at DPR 2; if these were master pixels,
raising `device_scale_factor` would silently halve every corner radius. `compose`
converts using the placement width, so a profile looks the same at any DPR.

ORDERING NOTE for the builder (Phase 4). `bordered` and `shadowed` GROW the
image, which changes its aspect ratio and therefore its placement. The builder
must therefore: compute a provisional placement from the master, compose with
that placement width, then recompute the placement from the *composed*
dimensions. Decoration is a few points, so the second pass converges at once.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

PT_PER_IN = 72.0


def _rgba(im):
    return im if im.mode == "RGBA" else im.convert("RGBA")


def px_per_pt(master_w_px: int, placement_w_in: float) -> float:
    """Master pixels per point at final placement size."""
    if not placement_w_in or not master_w_px:
        return 1.0
    return master_w_px / (placement_w_in * PT_PER_IN)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def pad_to_aspect(im, aspect: float, background="#FFFFFF", anchor="center"):
    """Letterbox onto a canvas of the given width/height ratio. Never crops."""
    from PIL import Image
    w, h = im.size
    target_w, target_h = w, h
    if w / h > aspect:
        target_h = max(h, int(round(w / aspect)))
    else:
        target_w = max(w, int(round(h * aspect)))
    if (target_w, target_h) == (w, h):
        return im
    canvas = Image.new("RGBA", (target_w, target_h), background)
    y = 0 if anchor == "top" else (target_h - h) // 2
    canvas.paste(im, ((target_w - w) // 2, y), im)
    return canvas


def crop_to_aspect(im, aspect: float, anchor="top"):
    """Crop to the given ratio. LOSSY — only ever reached via fit='crop-top',
    which a profile must opt into explicitly."""
    w, h = im.size
    if w / h > aspect:
        new_w = int(round(h * aspect))
        left = (w - new_w) // 2
        return im.crop((left, 0, left + new_w, h))
    new_h = int(round(w / aspect))
    top = 0 if anchor == "top" else (h - new_h) // 2
    return im.crop((0, top, w, top + new_h))


# --------------------------------------------------------------------------- #
# Decoration
# --------------------------------------------------------------------------- #
def rounded(im, radius_px: float):
    """Round the corners by punching an alpha mask. Supersampled 4x so the arc
    is smooth at any size."""
    from PIL import Image, ImageDraw
    r = max(0.0, float(radius_px))
    if r < 0.5:
        return im
    im = _rgba(im)
    w, h = im.size
    r = min(r, min(w, h) / 2.0)
    ss = 4
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=r * ss, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    out = im.copy()
    out.putalpha(mask)
    return out


def bordered(im, width_px: float, color="#E1E8ED", radius_px: float = 0.0):
    """Draw a hairline OUTSIDE the image, growing it by width on every side."""
    from PIL import Image, ImageDraw
    bw = max(0.0, float(width_px))
    if bw < 0.5:
        return im
    im = _rgba(im)
    b = int(round(bw))
    w, h = im.size
    canvas = Image.new("RGBA", (w + 2 * b, h + 2 * b), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    outer_r = (radius_px + bw) if radius_px >= 0.5 else 0
    if outer_r:
        draw.rounded_rectangle((0, 0, canvas.width - 1, canvas.height - 1),
                               radius=outer_r, fill=color)
    else:
        draw.rectangle((0, 0, canvas.width - 1, canvas.height - 1), fill=color)
    canvas.paste(im, (b, b), im)
    return canvas


def shadowed(im, blur_px: float, opacity: float = 0.18, dy_px: float = 0.0):
    """Drop shadow beneath the image, growing the canvas to hold it."""
    from PIL import Image, ImageFilter
    blur = max(0.0, float(blur_px))
    if blur < 0.5 or opacity <= 0:
        return im
    im = _rgba(im)
    pad = int(round(blur * 2)) + int(round(abs(dy_px)))
    w, h = im.size
    canvas = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    alpha = im.split()[3].point(lambda a: int(a * max(0.0, min(1.0, opacity))))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    shadow.paste(black, (pad, pad + int(round(dy_px))), alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    canvas = Image.alpha_composite(canvas, shadow)
    canvas.paste(im, (pad, pad), im)
    return canvas


def flatten(im, background="#FFFFFF"):
    """Composite onto an opaque background. Always last."""
    from PIL import Image
    im = _rgba(im)
    canvas = Image.new("RGBA", im.size, background)
    return Image.alpha_composite(canvas, im).convert("RGB")


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def is_identity(spec: dict) -> bool:
    """True when this image spec cannot change a single pixel.

    The two existing report types must pass through `compose` untouched — that
    is what makes routing them through the profile engine safe. This predicate
    is what the parity test asserts, and it is cheap enough for the builder to
    skip the work entirely.
    """
    if spec.get("aspect") and spec.get("fit") in ("pad", "crop-top"):
        return False
    if (spec.get("radius_pt") or 0) > 0:
        return False
    border = spec.get("border") or {}
    if (border.get("pt") or 0) > 0:
        return False
    shadow = spec.get("shadow") or {}
    if (shadow.get("blur_pt") or 0) > 0 and (shadow.get("opacity") or 0) > 0:
        return False
    if spec.get("watermark"):
        return False
    return True


def compose(im, spec: dict, placement_w_in: float = None):
    """Apply an image spec to one master screenshot.

    Fixed order — geometry, then decoration, then flatten — so a profile cannot
    accidentally depend on op ordering.
    """
    if is_identity(spec):
        return im                      # untouched, bit for bit

    scale = px_per_pt(im.size[0], placement_w_in) if placement_w_in else 1.0
    bg = spec.get("background") or "#FFFFFF"
    aspect = registry.parse_aspect(spec.get("aspect"))
    fit_mode = spec.get("fit", "fit")

    out = _rgba(im)
    if aspect and fit_mode == "pad":
        out = pad_to_aspect(out, aspect, bg)
    elif aspect and fit_mode == "crop-top":
        out = crop_to_aspect(out, aspect, anchor="top")

    radius_px = (spec.get("radius_pt") or 0) * scale
    if radius_px >= 0.5:
        out = rounded(out, radius_px)

    border = spec.get("border") or {}
    if (border.get("pt") or 0) > 0:
        out = bordered(out, border["pt"] * scale,
                       border.get("color", "#E1E8ED"), radius_px)

    shadow = spec.get("shadow") or {}
    if (shadow.get("blur_pt") or 0) > 0:
        out = shadowed(out, shadow["blur_pt"] * scale,
                       shadow.get("opacity", 0.18),
                       (shadow.get("dy_pt") or 0) * scale)

    return flatten(out, bg)

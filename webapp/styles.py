"""Report styles designed in the web app — the "style designer".

A style is a profile JSON (docs/profile-engine.md) saved under
`DATA_DIR/profiles/`, where `profiles/registry.py` reads it AFTER the shipped
registry. Everything the designer submits goes through `registry.resolve()`,
the same merge + validation a file on disk gets, so a saved style can never be
one the runner refuses — unknown keys, impossible margins, an engine that
cannot produce the metrics asked for, all fail here with the registry's own
message.

What is deliberately NOT possible from here:

  * touching the two built-in reports (`twitter`, `influencer`) or any shipped
    profile — those slugs are reserved and their files live in the code tree;
  * naming a capture knob `capture()` does not take — the registry rejects it;
  * a slug that is not a plain `[a-z0-9-]` word — the slug becomes a filename
    and a CLI argument, so it is validated as one.

RULEBOOK rule 3 still applies to anything designed here: the thumbnail is the
profile's real geometry, but look at the first real PDF before trusting it.
"""
import io
import json
import re
import shutil
import sys

from . import config, previews, report_types

_PROFILES = config.ROOT / "profiles"
if str(_PROFILES) not in sys.path:
    sys.path.insert(0, str(_PROFILES))

import registry      # noqa: E402  (profiles/registry.py)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

# What the designer may set. Anything else in the payload is dropped before
# validation, so a hostile body cannot smuggle a key past the registry's
# allow-list by naming it at the top level.
_KEEP_TOP = ("schema", "slug", "label", "description", "extends", "platform",
             "capture", "image", "page", "content", "outputs")


class StyleError(ValueError):
    pass


def slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s[:40]


def reserved_slugs() -> set:
    """Built-ins plus every shipped profile — never writable from the app."""
    shipped = {p.stem for p in registry.REGISTRY_DIR.glob("*.json")}
    return shipped | {rt.slug for rt in report_types.all_types() if not rt.custom}


def _clean(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise StyleError("The style must be a JSON object.")
    p = {k: raw[k] for k in _KEEP_TOP if k in raw}
    p["schema"] = registry.SCHEMA_VERSION
    p["slug"] = str(p.get("slug") or slugify(p.get("label", ""))).strip()
    p["label"] = str(p.get("label") or "").strip()
    if not p["label"]:
        raise StyleError("Give the style a name.")
    if not _SLUG.match(p["slug"]):
        raise StyleError("The style id must be 2–40 characters: lowercase "
                         "letters, digits and hyphens.")
    return p


def resolve(raw: dict) -> dict:
    """Cleaned + fully-resolved + validated profile, or StyleError."""
    p = _clean(raw)
    try:
        return registry.resolve(p)
    except registry.ProfileError as e:
        raise StyleError(str(e)) from e


def _path(slug: str):
    return config.USER_PROFILES_DIR / f"{slug}.json"


def list_custom() -> list:
    """Every user-designed style, resolved, in name order."""
    out = []
    for path in sorted(config.USER_PROFILES_DIR.glob("*.json")):
        try:
            out.append({"slug": path.stem, "raw": json.loads(path.read_text()),
                        "resolved": registry.load(path.stem)})
        except Exception as e:                       # rule 17: say so, keep going
            print(f"[styles] skipping {path.name}: {e}", flush=True)
    return sorted(out, key=lambda s: s["resolved"]["label"].lower())


def get_raw(slug: str) -> dict:
    """The stored (un-merged) JSON of a user style, or the resolved profile of
    a shipped one — what the designer loads to 'duplicate' from."""
    if not _SLUG.match(slug or ""):
        raise StyleError("Unknown style.")
    p = _path(slug)
    if p.exists():
        return json.loads(p.read_text())
    try:
        return registry.load(slug)
    except registry.ProfileError as e:
        raise StyleError(str(e)) from e


def save(raw: dict, overwrite: bool = False) -> dict:
    """Validate, write, refresh thumbnails. Returns the resolved profile."""
    p = _clean(raw)
    if p["slug"] in reserved_slugs():
        raise StyleError(f"'{p['slug']}' is a built-in style and cannot be "
                         "changed. Pick another name.")
    if p.get("extends") == p["slug"]:
        raise StyleError("A style cannot extend itself.")
    resolved = resolve(p)
    dest = _path(p["slug"])
    if dest.exists() and not overwrite:
        raise StyleError(f"A style called '{p['slug']}' already exists. "
                         "Tick 'replace' to overwrite it.")
    config.USER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2))
    tmp.replace(dest)                    # atomic: never a half-written profile
    previews.refresh()
    return resolved


def delete(slug: str) -> bool:
    if not _SLUG.match(slug or "") or slug in reserved_slugs():
        raise StyleError("That style cannot be deleted.")
    p = _path(slug)
    if not p.exists():
        return False
    p.unlink()
    shutil.rmtree(asset_dir(slug), ignore_errors=True)
    previews.refresh()
    return True


def preview_png(raw: dict, width: int = 240) -> bytes:
    """A thumbnail of an UNSAVED style, drawn by the same code that draws the
    dashboard cards — so what the designer shows is what the page will be."""
    raw = dict(raw or {})
    raw.setdefault("label", "")
    if not str(raw.get("label") or "").strip():
        raw["label"] = "Untitled style"           # a preview needs no name yet
    if not str(raw.get("slug") or "").strip():
        raw["slug"] = "preview"
    resolved = resolve(raw)
    import thumbnails                    # profiles/thumbnails.py
    im = thumbnails.render(resolved, page_w=max(120, min(int(width), 700)))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Which styles appear on the New report page — the admin's curation.
# Built-ins are always shown. Shipped profiles are shown unless hidden. Styles
# designed in the app are PENDING until an admin approves them, so a designer
# can iterate freely without every draft landing in front of colleagues.
# --------------------------------------------------------------------------- #
_SETTINGS = config.DATA_DIR / "style_settings.json"


def _settings() -> dict:
    try:
        d = json.loads(_SETTINGS.read_text())
        return {"approved": list(d.get("approved", [])), "hidden": list(d.get("hidden", []))}
    except (OSError, ValueError):
        return {"approved": [], "hidden": []}


def _save_settings(d: dict) -> None:
    _SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(_SETTINGS)


def visibility(rt) -> str:
    """'live' | 'hidden' | 'pending' for a ReportType."""
    d = _settings()
    if rt.custom:
        return "live" if rt.slug in d["approved"] else "pending"
    return "hidden" if rt.slug in d["hidden"] else "live"


def set_visible(slug: str, show: bool) -> str:
    rt = report_types.get(slug)
    if rt is None:
        raise StyleError("Unknown style.")
    if not show and len(visible_types()) <= 1 and visibility(rt) == "live":
        raise StyleError("That is the last style on New report — show another "
                         "one before hiding this.")
    d = _settings()
    if rt.custom:
        d["approved"] = [x for x in d["approved"] if x != slug] + ([slug] if show else [])
    else:
        d["hidden"] = [x for x in d["hidden"] if x != slug] + ([] if show else [slug])
    _save_settings(d)
    return visibility(rt)


def visible_types() -> list:
    """What the New report page offers, in report_types order."""
    return [rt for rt in report_types.all_types() if visibility(rt) == "live"]


# --------------------------------------------------------------------------- #
# Template styles — designed pages (Canva PNGs) + slots drawn in the app
# --------------------------------------------------------------------------- #
_MAX_PAGE_PX = 2600            # ~300 dpi on A4 — plenty for print, small on disk
_ALLOWED_IMG = ("PNG", "JPEG")


def asset_dir(slug: str):
    return config.USER_PROFILES_DIR / "assets" / slug


def _store_page(slug: str, kind: str, data: bytes) -> str:
    """Validate + normalise an uploaded page image; return its filename."""
    from PIL import Image, UnidentifiedImageError
    if len(data) > 12 * 1024 * 1024:
        raise StyleError(f"The {kind} page image is over 12 MB.")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError):
        raise StyleError(f"The {kind} page image is not a PNG/JPEG.")
    if im.format not in _ALLOWED_IMG:
        raise StyleError(f"The {kind} page must be PNG or JPEG (got {im.format}).")
    if im.width < 300 or im.height < 300:
        raise StyleError(f"The {kind} page image is too small ({im.width}×{im.height}).")
    if max(im.size) > _MAX_PAGE_PX:
        ratio = _MAX_PAGE_PX / max(im.size)
        im = im.resize((round(im.width * ratio), round(im.height * ratio)), Image.LANCZOS)
    im = im.convert("RGB")
    d = asset_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{kind}.png"
    im.save(d / name, "PNG", optimize=True)
    return name


def save_template(meta: dict, files: dict, overwrite: bool = False) -> dict:
    """`meta`: label, slug, base (engine profile), paper, orientation, slots,
    text, outputs, links_table, description. `files`: {"post": bytes,
    "cover": bytes|None, "end": bytes|None}. Returns the resolved profile."""
    label = str(meta.get("label") or "").strip()
    slug = str(meta.get("slug") or slugify(label)).strip()
    if not label:
        raise StyleError("Give the style a name.")
    if not _SLUG.match(slug):
        raise StyleError("The style id must be 2–40 characters: lowercase letters, digits and hyphens.")
    if slug in reserved_slugs():
        raise StyleError(f"'{slug}' is a built-in style. Pick another name.")
    dest = _path(slug)
    if dest.exists() and not overwrite:
        raise StyleError(f"A style called '{slug}' already exists. Tick 'replace' to overwrite it.")
    base = str(meta.get("base") or "twitter")
    try:
        base_p = registry.load(base)
    except registry.ProfileError as e:
        raise StyleError(f"Unknown base engine profile: {e}")

    existing = json.loads(dest.read_text()) if dest.exists() else {}
    pages = dict(((existing.get("template") or {}).get("pages") or {})) if overwrite else {}
    for kind in ("post", "cover", "end"):
        data = files.get(kind)
        if data:
            pages[kind] = _store_page(slug, kind, data)
        elif meta.get(f"remove_{kind}"):
            pages.pop(kind, None)
    if not pages.get("post"):
        raise StyleError("Upload the post page image (the page a screenshot goes on).")

    slots = meta.get("slots") or []
    text = meta.get("text") or []
    outputs = [o for o in (meta.get("outputs") or ["pdf"]) if o in registry.OUTPUTS] or ["pdf"]
    paper = str(meta.get("paper") or "a4").lower()
    if paper not in registry.PAGE_SIZES:
        raise StyleError("Paper must be letter or A4.")
    orient = "landscape" if str(meta.get("orientation")) == "landscape" else "portrait"
    pw, ph = registry.PAGE_SIZES[paper]
    if orient == "landscape":
        pw, ph = ph, pw
    # image.max_in is informational for template styles (slots decide), but the
    # schema requires it — record the largest slot in inches.
    max_w = max((float(sl.get("w", 0)) for sl in slots), default=0.5) * pw
    max_h = max((float(sl.get("h", 0)) for sl in slots), default=0.5) * ph

    p = {
        "schema": 1, "slug": slug, "label": label, "extends": base,
        "description": str(meta.get("description") or f"Designed page template on {paper.upper()}."),
        "capture": {"keep_engagement": bool(meta.get("keep_engagement", base_p["capture"].get("keep_engagement")))}
                   if base_p["capture"]["engine"] == "x" else {},
        "image": {"max_in": [round(max_w, 3), round(max_h, 3)], "aspect": None, "fit": "fit",
                  "background": "#FFFFFF", "radius_pt": float(meta.get("radius_pt") or 0),
                  "border": None, "shadow": None},
        "page": {"size": paper, "orientation": orient, "grid": [1, 1],
                 "margins_in": [0.5, 0.5, 0.5, 0.5]},
        "content": {"cover": False, "header": None, "footer": None,
                    "per_post_fields": [], "metrics": None,
                    "links_table": bool(meta.get("links_table", True))},
        "template": {"pages": pages, "slots": slots, "text": text},
        "outputs": outputs,
    }
    resolved = resolve(p)                # registry validates slots/text/pages
    config.USER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2))
    tmp.replace(dest)
    previews.refresh()
    return resolved


def designer_options() -> dict:
    """Static vocab for the designer form, straight from the registry so the UI
    can never offer a value the validator refuses."""
    return {
        "page_sizes": sorted(registry.PAGE_SIZES),
        "fits": list(registry.FITS),
        "outputs": list(registry.OUTPUTS),
        "engines": {k: v for k, v in registry.ENGINES.items()},
        "bases": [{"slug": s, "label": registry.load(s)["label"],
                   "engine": registry.load(s)["capture"]["engine"]}
                  for s in registry.available()],
        "reserved": sorted(reserved_slugs()),
        "metrics_keys": ["followers", "reactions", "comments", "reach", "shares"],
        "per_post_fields": ["account_name", "post_link", "category"],
    }

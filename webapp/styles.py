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

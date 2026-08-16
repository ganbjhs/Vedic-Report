"""Load, merge and validate report profiles.

A profile is the *presentation* of a report expressed as data: page size, N-up
grid, image shape, framing, outputs. See docs/profile-engine.md.

Two rules this module exists to enforce, both learned the hard way:

  * **Unknown keys are an error, never ignored.** A typo'd "radius-px" that
    silently does nothing is the same class of bug as RULEBOOK rule 20 — a
    setting that looks applied and is not.
  * **No profile may name a capture knob that `capture()` does not take.**
    `thread_ancestors` is deliberately absent; if it is ever wanted it gets a
    proper approved edit, not a monkey-patch (design note §4.2).

Nothing here imports Playwright, opens a browser or reads a screenshot, so the
whole module is testable with no captures.
"""
import copy
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY_DIR = HERE / "registry"

# A second, OPTIONAL registry for profiles people design in the web app's style
# designer. Kept out of the code tree (it is runtime state, like jobs), and
# consulted AFTER the in-repo registry so a user profile can never shadow a
# shipped one. `None` = feature off; the CLI never needs it. Inside a job the
# runner copies the chosen user profile into the job's private
# `profiles/registry/`, so the subprocess reads it through the normal path and
# rule 2's isolation still holds.
USER_REGISTRY_DIR = None

SCHEMA_VERSION = 1

# Engines a profile may select, and what each one's result dict carries.
ENGINES = {
    "x": {"metrics": False, "platform": "x"},              # src/capture/x_capture.py
    "influencer": {"metrics": True, "platform": "x"},      # influencer/inf_capture.py
    "facebook": {"metrics": False, "platform": "facebook"},  # facebook/fb_capture.py
}

# Which network a profile's posts come from. NOT the same axis as `engine`:
# both engines above are X engines — one crops the engagement bar, the other
# keeps it and adds metrics. A second platform means a new rule-18 folder and a
# new entry in BOTH this tuple and `PLATFORMS` in webapp/report_types.py, which
# owns how the pill looks and whether it is live. This list is deliberately
# permissive of not-yet-live platforms so a profile can be written and validated
# before its engine exists; the web layer refuses to *run* it.
PLATFORMS = ("x", "facebook", "instagram")
DEFAULT_PLATFORM = "x"

# Page sizes in inches, portrait.
PAGE_SIZES = {"letter": (8.5, 11.0), "a4": (8.2677, 11.6929)}

OUTPUTS = ("pdf", "docx", "html")         # xlsx is a global export, not a profile output
FITS = ("fit", "pad", "crop-top")

# Every key any section may contain. Anything else raises.
_ALLOWED = {
    "capture": {"engine", "device_scale_factor", "viewport", "keep_engagement",
                "workers"},
    "image": {"max_in", "aspect", "fit", "background", "radius_pt", "border",
              "shadow", "watermark"},
    "page": {"size", "orientation", "grid", "margins_in"},
    "content": {"cover", "header", "footer", "per_post_fields", "metrics",
                "links_table"},
}
_TOP = {"schema", "slug", "label", "description", "extends", "platform",
        "capture", "image", "page", "content", "outputs"}

# Capture knobs that DO NOT EXIST, with the reason, so a typo gets a real answer
# instead of "unknown key".
_REJECTED_CAPTURE = {
    "thread_ancestors": "deliberately absent from v1 — it would require "
                        "mutating x_capture module state. If a profile needs "
                        "thread depth, ask for approved edit 6 "
                        "(capture(..., thread_ancestors=None)) first. "
                        "See docs/profile-engine.md §4.2.",
}


class ProfileError(ValueError):
    """A profile that cannot be trusted to render. Always names the slug."""


# --------------------------------------------------------------------------- #
# Load + merge
# --------------------------------------------------------------------------- #
def set_user_dir(path) -> None:
    """Point the optional user registry at `path` (or None to turn it off)."""
    global USER_REGISTRY_DIR
    USER_REGISTRY_DIR = Path(path) if path else None


def _dirs(registry_dir: Path) -> list:
    dirs = [registry_dir]
    if USER_REGISTRY_DIR and USER_REGISTRY_DIR != registry_dir:
        dirs.append(USER_REGISTRY_DIR)
    return dirs


def _path_for(slug: str, registry_dir: Path):
    for d in _dirs(registry_dir):
        p = d / f"{slug}.json"
        if p.exists():
            return p
    return None


def is_user(slug: str, registry_dir: Path = None) -> bool:
    """True when `slug` comes from the user registry, not the shipped one."""
    registry_dir = registry_dir or REGISTRY_DIR
    p = _path_for(slug, registry_dir)
    return bool(p and USER_REGISTRY_DIR and p.parent == USER_REGISTRY_DIR)


def _read(slug: str, registry_dir: Path) -> dict:
    path = _path_for(slug, registry_dir)
    if path is None:
        raise ProfileError(f"no such profile: {slug!r} (looked in "
                           f"{', '.join(str(d) for d in _dirs(registry_dir))})")
    try:
        return json.loads(path.read_text())
    except ValueError as e:
        raise ProfileError(f"{slug}: not valid JSON — {e}") from e


def _merge(base: dict, over: dict) -> dict:
    """Shallow-merge per section: `extends` overrides individual keys, so a
    child profile's diff IS its design."""
    out = copy.deepcopy(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **val}
        else:
            out[key] = copy.deepcopy(val)
    return out


def load(slug: str, registry_dir: Path = None, _seen=None) -> dict:
    """Fully-resolved, validated profile for `slug`."""
    registry_dir = registry_dir or REGISTRY_DIR
    _seen = _seen or []
    if slug in _seen:
        raise ProfileError(f"circular extends: {' -> '.join(_seen + [slug])}")

    raw = _read(slug, registry_dir)
    parent = raw.get("extends")
    if parent:
        base = load(parent, registry_dir, _seen + [slug])
        # The child's own identity must never be inherited from the parent.
        base.pop("extends", None)
        raw = _merge(base, {k: v for k, v in raw.items() if k != "extends"})
        raw["slug"] = _read(slug, registry_dir)["slug"]
    validate(raw)
    return raw


def resolve(raw: dict, registry_dir: Path = None) -> dict:
    """Resolve + validate an in-memory profile (one not yet saved to disk).

    Same merge as `load`, so what the style designer previews is exactly what
    a saved file would load as. `extends` may only name a profile that already
    exists on disk — a chain of unsaved profiles is not a thing.
    """
    registry_dir = registry_dir or REGISTRY_DIR
    if not isinstance(raw, dict):
        raise ProfileError("a profile must be a JSON object")
    parent = raw.get("extends")
    if parent:
        if not isinstance(parent, str):
            raise ProfileError("extends must be a profile slug")
        base = load(parent, registry_dir)
        base.pop("extends", None)
        merged = _merge(base, {k: v for k, v in raw.items() if k != "extends"})
        merged["slug"] = raw.get("slug")
    else:
        merged = copy.deepcopy(raw)
    validate(merged)
    return merged


def available(registry_dir: Path = None) -> list:
    """Slugs of every profile that loads and validates — shipped ones first
    (sorted), then user-designed ones (sorted).

    A broken profile is skipped rather than taking the whole app down — but it
    is reported, because a silent disappearance is worse than a loud one
    (RULEBOOK rule 17: log every failure branch).
    """
    registry_dir = registry_dir or REGISTRY_DIR
    good, seen = [], set()
    for d in _dirs(registry_dir):
        for path in sorted(d.glob("*.json")):
            if path.stem in seen:
                continue                 # shipped registry wins on collision
            try:
                load(path.stem, registry_dir)
                good.append(path.stem)
                seen.add(path.stem)
            except ProfileError as e:
                print(f"[profiles] IGNORING broken profile {path.name}: {e}",
                      flush=True)
    return good


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #
def _unknown(section: str, got: dict, slug: str) -> None:
    extra = set(got) - _ALLOWED[section]
    if not extra:
        return
    hints = [f"{k!r}: {_REJECTED_CAPTURE[k]}" for k in sorted(extra)
             if k in _REJECTED_CAPTURE]
    detail = "; ".join(hints) if hints else (
        f"allowed: {', '.join(sorted(_ALLOWED[section]))}")
    raise ProfileError(f"{slug}: unknown key(s) in '{section}': "
                       f"{', '.join(sorted(extra))} — {detail}")


def _num(val, name, slug, lo=None, hi=None):
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise ProfileError(f"{slug}: {name} must be a number, got {val!r}")
    if lo is not None and val < lo:
        raise ProfileError(f"{slug}: {name} must be >= {lo}, got {val}")
    if hi is not None and val > hi:
        raise ProfileError(f"{slug}: {name} must be <= {hi}, got {val}")
    return val


def parse_aspect(aspect):
    """'4:5' -> 0.8 (width/height). None -> None."""
    if aspect is None:
        return None
    try:
        w, h = str(aspect).split(":")
        ratio = float(w) / float(h)
    except (ValueError, ZeroDivisionError) as e:
        raise ProfileError(f"aspect must look like 'W:H', got {aspect!r}") from e
    if ratio <= 0:
        raise ProfileError(f"aspect must be positive, got {aspect!r}")
    return ratio


def page_inches(page: dict) -> tuple:
    """(width_in, height_in) honouring orientation."""
    size = page.get("size", "letter")
    if isinstance(size, (list, tuple)):
        w, h = float(size[0]), float(size[1])
    else:
        key = str(size).lower()
        if key not in PAGE_SIZES:
            raise ProfileError(f"unknown page size {size!r}; "
                               f"use one of {sorted(PAGE_SIZES)} or [w_in, h_in]")
        w, h = PAGE_SIZES[key]
    if str(page.get("orientation", "portrait")).lower() == "landscape":
        w, h = h, w
    return w, h


def validate(p: dict) -> dict:
    """Raise ProfileError on anything that would render wrongly or silently."""
    slug = p.get("slug") or "<no slug>"

    extra_top = set(p) - _TOP
    if extra_top:
        raise ProfileError(f"{slug}: unknown top-level key(s): "
                           f"{', '.join(sorted(extra_top))}")
    if p.get("schema") != SCHEMA_VERSION:
        raise ProfileError(f"{slug}: schema must be {SCHEMA_VERSION}, "
                           f"got {p.get('schema')!r}")
    if not isinstance(p.get("slug"), str) or not p["slug"]:
        raise ProfileError("every profile needs a non-empty string slug")
    if not isinstance(p.get("label"), str) or not p["label"]:
        raise ProfileError(f"{slug}: needs a non-empty label")
    if p.get("platform", DEFAULT_PLATFORM) not in PLATFORMS:
        raise ProfileError(f"{slug}: unknown platform "
                           f"{p.get('platform')!r}; allowed: {list(PLATFORMS)}")

    for section in ("capture", "image", "page", "content"):
        if not isinstance(p.get(section), dict):
            raise ProfileError(f"{slug}: missing '{section}' section")
        _unknown(section, p[section], slug)

    cap = p["capture"]
    if cap.get("engine") not in ENGINES:
        raise ProfileError(f"{slug}: capture.engine must be one of "
                           f"{sorted(ENGINES)}, got {cap.get('engine')!r}")
    # An engine captures ONE network. A profile that says platform=facebook
    # with the X engine would show Facebook links to a capture that cannot
    # read them — refuse it here, not deep in a worker.
    want = ENGINES[cap["engine"]]["platform"]
    if p.get("platform", DEFAULT_PLATFORM) != want:
        raise ProfileError(f"{slug}: capture.engine {cap['engine']!r} captures "
                           f"{want!r} posts, but platform is "
                           f"{p.get('platform', DEFAULT_PLATFORM)!r}")
    _num(cap.get("device_scale_factor", 1), "capture.device_scale_factor",
         slug, 1, 3)
    vp = cap.get("viewport") or {}
    if not isinstance(vp, dict) or "width" not in vp or "height" not in vp:
        raise ProfileError(f"{slug}: capture.viewport needs width and height")
    # RULEBOOK rule 4: below ~800px X paints its nav rail over the tweet column
    # and no crop can remove it. Refuse rather than produce ruined screenshots.
    _num(vp["width"], "capture.viewport.width", slug, 900, 3000)
    _num(vp["height"], "capture.viewport.height", slug, 600, 6000)
    if not isinstance(cap.get("keep_engagement", False), bool):
        raise ProfileError(f"{slug}: capture.keep_engagement must be true/false")
    if cap.get("workers") is not None:
        _num(cap["workers"], "capture.workers", slug, 1, 16)

    img = p["image"]
    box = img.get("max_in")
    if not (isinstance(box, (list, tuple)) and len(box) == 2):
        raise ProfileError(f"{slug}: image.max_in must be [width_in, height_in] "
                           "— it is not derivable from the page, see "
                           "docs/profile-engine.md §5.4")
    _num(box[0], "image.max_in[0]", slug, 0.25)
    _num(box[1], "image.max_in[1]", slug, 0.25)
    parse_aspect(img.get("aspect"))
    if img.get("fit", "fit") not in FITS:
        raise ProfileError(f"{slug}: image.fit must be one of {FITS}, "
                           f"got {img.get('fit')!r}")
    if img.get("aspect") is None and img.get("fit") in ("pad", "crop-top"):
        raise ProfileError(f"{slug}: image.fit={img['fit']!r} needs an aspect "
                           "to pad or crop to")
    _num(img.get("radius_pt", 0), "image.radius_pt", slug, 0)
    if img.get("border"):
        _num(img["border"].get("pt", 0), "image.border.pt", slug, 0)
    if img.get("shadow"):
        _num(img["shadow"].get("blur_pt", 0), "image.shadow.blur_pt", slug, 0)
        _num(img["shadow"].get("opacity", 0), "image.shadow.opacity", slug, 0, 1)

    page = p["page"]
    pw, ph = page_inches(page)
    grid = page.get("grid", [1, 1])
    if not (isinstance(grid, (list, tuple)) and len(grid) == 2
            and all(isinstance(g, int) and g >= 1 for g in grid)):
        raise ProfileError(f"{slug}: page.grid must be [cols, rows], "
                           f"both >= 1, got {grid!r}")
    margins = page.get("margins_in", [0.6] * 4)
    if not (isinstance(margins, (list, tuple)) and len(margins) == 4):
        raise ProfileError(f"{slug}: page.margins_in must be "
                           "[top, right, bottom, left]")
    for m in margins:
        _num(m, "page.margins_in", slug, 0)
    if margins[1] + margins[3] >= pw or margins[0] + margins[2] >= ph:
        raise ProfileError(f"{slug}: margins leave no content area on a "
                           f"{pw:.2f}x{ph:.2f} in page")

    content = p["content"]
    metrics = content.get("metrics")
    if metrics is not None:
        if not ENGINES[cap["engine"]]["metrics"]:
            raise ProfileError(
                f"{slug}: content.metrics is set but capture.engine="
                f"{cap['engine']!r} never produces metrics — use the "
                "'influencer' engine or drop content.metrics")
        if not (isinstance(metrics, list) and metrics
                and all(isinstance(m, (list, tuple)) and len(m) == 2
                        for m in metrics)):
            raise ProfileError(f"{slug}: content.metrics must be a non-empty "
                               "list of [label, key] pairs")

    outs = p.get("outputs") or []
    if not outs:
        raise ProfileError(f"{slug}: needs at least one output")
    bad = [o for o in outs if o not in OUTPUTS]
    if bad:
        extra = ("  ('xlsx' is a global data export, not a profile output — "
                 "see docs/profile-engine.md §10)" if "xlsx" in bad else "")
        raise ProfileError(f"{slug}: unsupported output(s) {bad}; "
                           f"allowed: {list(OUTPUTS)}.{extra}")
    return p


def per_page(profile: dict) -> int:
    cols, rows = profile["page"]["grid"]
    return cols * rows

"""Profile preview thumbnails + sample documents for the dashboard cards.

Thin web-side adapter over `profiles/thumbnails.py`: it decides WHERE the images
live and answers "what is the current filename for this slug", so the template
never has to know about hashing or regeneration.

Kept out of `profiles/` because the static directory and the sample-download
policy are web concerns, and `profiles/` must stay runnable from the CLI with no
webapp import.
"""
import sys
from pathlib import Path

from . import config

_PROFILES = config.ROOT / "profiles"
if str(_PROFILES) not in sys.path:
    sys.path.insert(0, str(_PROFILES))

STATIC_DIR = Path(__file__).resolve().parent / "static" / "profiles"
SAMPLES_DIR = config.ROOT / "data" / "samples"

_MANIFEST = {}


def refresh() -> dict:
    """Regenerate any thumbnail whose profile changed. Safe to call at boot."""
    global _MANIFEST
    try:
        import thumbnails
        _MANIFEST = thumbnails.generate(STATIC_DIR)
    except Exception as e:                    # a broken preview must not stop the app
        print(f"[previews] thumbnail generation skipped: {e}", flush=True)
        _MANIFEST = {}
    return _MANIFEST


def manifest() -> dict:
    """{slug: {"img": url or None, "sample": url or None}} for the template."""
    out = {}
    for slug, name in (_MANIFEST or {}).items():
        out[slug] = {
            "img": f"/static/profiles/{name}",
            "sample": f"/samples/{slug}.pdf" if sample_path(slug) else None,
        }
    return out


def sample_path(slug: str):
    """The sample PDF for `slug`, or None. Slug is matched against known
    profiles — never interpolated into a path from user input."""
    try:
        import registry
        known = set(registry.available())
    except Exception:
        known = set()
    if slug not in known:
        return None
    path = SAMPLES_DIR / f"{slug}.pdf"
    return path if path.is_file() else None

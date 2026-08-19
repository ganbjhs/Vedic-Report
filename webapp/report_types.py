"""What report types exist, and what each one can do.

WHY THIS FILE. The web layer used to decide everything about a report type with
`report_type != "twitter"` and four more `== "twitter"` tests. That is a binary
where a table is needed: add a third slug without fixing it and the job
**silently runs the influencer report** (docs/profile-engine.md §7). Capability
is now data — the form and `build_command` branch on flags, not on a slug.

Two sources, deliberately kept separate:

  * **Built-ins** — `twitter` and `influencer` keep their own proven
    entrypoints (`run.py`, `influencer/run_influencer.py`). They are NOT routed
    through the profile engine; that was decision 3 in the design note, and it
    is why adding profiles cannot regress the reports you depend on.
  * **Profiles** — everything in `profiles/registry/` other than those two,
    invoked via `profiles/run_profile.py --profile <slug>`.

A broken profile is skipped with a log line rather than taking the app down
(RULEBOOK rule 17: log every failure branch), so one bad JSON file cannot stop
anyone submitting a Twitter report.

THE PLATFORM AXIS. A report type answers two separate questions: *which network
the posts come from* (platform) and *how the page is laid out* (style). Those
were one thing while X was the only network, and folding them together is how
you get the `report_type != "twitter"` bug a second time — on a new axis.

`PLATFORMS` is that second axis as a table. A platform is `live` only when a
capture engine actually exists behind it; the rest render as pills with a badge
and are refused at the API boundary, not merely disabled in HTML. Adding
Facebook later is one `Platform` entry here plus its rule-18 folder — the form,
the gate and the grouping all read the table.
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config

_PROFILES_DIR = config.ROOT / "profiles"
if str(_PROFILES_DIR) not in sys.path:
    sys.path.insert(0, str(_PROFILES_DIR))

# Styles designed in the web app live under DATA_DIR and are read by the same
# registry, after the shipped ones. Set once, here, because this module is the
# first web-side importer of `registry`; previews/thumbnails share the module.
try:
    import registry as _registry
    _registry.set_user_dir(config.USER_PROFILES_DIR)
except Exception as e:                                  # pragma: no cover
    print(f"[report-types] user profile registry not wired: {e}", flush=True)


@dataclass(frozen=True)
class Platform:
    """One network the app can capture from — or will be able to.

    `live` is the only gate that matters: it means "a capture engine exists and
    a job for this platform can really run". Everything else here is presentation.

    `combines` marks the pill that is not a network at all but the multi-platform
    mode: it turns the picker into a multi-select over the live platforms and
    takes one mixed link list. It is kept in this table rather than beside it so
    the form renders one row of pills from one loop.
    """
    slug: str
    label: str
    live: bool = False
    badge: str = ""             # shown on the pill when the platform is not live
    note: str = ""              # one line explaining why, for the tooltip
    combines: bool = False

    @property
    def enabled(self) -> bool:
        return self.live


_SOON = ("Capture engine not built yet — it lands as its own project, in its "
         "own folder, alongside the X one (RULEBOOK rule 18).")

PLATFORMS = (
    Platform("x", "X", live=True),
    # Live: facebook/fb_capture.py, run through the profile engine. Public
    # posts need no account; a saved sessions/fb_state.json is used if present.
    Platform("facebook", "Facebook", live=True),
    # Live: instagram/ig_capture.py — public posts, logged-out (the sign-in
    # panel is closed); sessions/ig_state.json is used if present.
    Platform("instagram", "Instagram", live=True),
    # Live: one mixed link list (X + Facebook + Instagram), sections from the
    # sheet, metrics from the sheet's Like / Impressions / Views / Reach
    # columns. Styles for it are profiles with capture.engine = "combined".
    Platform("combined", "Combined", live=True, combines=True,
             note="One report from X, Facebook and Instagram links together."),
)

DEFAULT_PLATFORM = "x"


@dataclass(frozen=True)
class ReportType:
    """One selectable report type.

    `argv` is the command, relative to the job's app directory. `worker_pool`
    names which server default applies. The two `allows_*` flags are what the
    form and `build_command` branch on — never the slug.
    """
    slug: str
    label: str
    argv: tuple
    worker_pool: str                 # "capture" | "influencer"
    allows_worker_choice: bool
    allows_keep_engagement: bool
    description: str = ""
    caption: str = ""            # one line under the thumbnail
    builtin: bool = False
    # Which documents THIS style can produce. Per style, deliberately: a
    # designed-page template renders as an exact PDF and an editable PPTX, a
    # numeric style as PDF + DOCX, and the two built-ins keep the PDF + DOCX
    # their own frozen builders have always written. There is no server-wide
    # format setting — a global one would either promise a deck for a style
    # with no page art, or hide a format the style really does build.
    outputs: tuple = field(default=("pdf", "docx"))
    # True for a style designed in the web app (data/profiles/), which may be
    # edited or deleted there; shipped profiles and built-ins may not.
    custom: bool = False
    # True for a designed-page (Canva) template style — edited in the template
    # designer, not the numeric one.
    template: bool = False
    # True when this style's entrypoint understands `--fast` (approved edit 6c).
    # A capability, not a slug test, for the reason this whole file exists: the
    # profile runners do not take the switch, and passing it to one would fail
    # the job on an unrecognised argument rather than merely be ignored.
    allows_fast: bool = False
    # Which network this style captures from. Everything today is X; a style is
    # offered only under its own platform, so the Style step never shows a card
    # that cannot run.
    platform: str = DEFAULT_PLATFORM

    def default_workers(self) -> int:
        return (config.INFLUENCER_WORKERS if self.worker_pool == "influencer"
                else config.CAPTURE_WORKERS)


_BUILTINS = (
    ReportType(
        slug="twitter", label="Twitter Report",
        argv=("run.py",), worker_pool="capture",
        allows_worker_choice=True, allows_keep_engagement=True,
        allows_fast=True, builtin=True,
        caption="Letter · 1 post per page",
        description="Clean tweet screenshots with the engagement bar cropped "
                    "out. One post per page, plus a links table.",
        outputs=("pdf", "docx")),
    ReportType(
        # One browser, always: the follower-count cache lives in the worker
        # PROCESS, so a second worker re-fetches the same profiles (rule 12).
        # Expressed as a capability flag so a crafted POST cannot override it.
        slug="influencer", label="Influencer Report",
        argv=("influencer/run_influencer.py",), worker_pool="influencer",
        allows_worker_choice=False, allows_keep_engagement=False, builtin=True,
        caption="A4 · 2 per page · with metrics",
        description="Keeps likes & reposts in the screenshot and adds a metrics "
                    "table: Reactions, Comments, Reach, Shares.",
        outputs=("pdf", "docx")),
)


def _caption(p: dict) -> str:
    """One line describing the page, derived from the profile itself so it can
    never drift from the thumbnail beside it."""
    page = p.get("page") or {}
    raw = str(page.get("size", "letter")).lower()
    size = {"letter": "Letter", "a4": "A4"}.get(raw, raw.title())
    tpl = p.get("template") or {}
    if tpl:
        n = len(tpl.get("slots") or [])
    else:
        cols, rows = (page.get("grid") or [1, 1])[:2]
        n = cols * rows
    bits = [size, f"{n} per page" if n > 1 else "1 post per page"]
    if tpl:
        bits.append("designed page")
        if (tpl.get("pages") or {}).get("cover"):
            bits.append("cover")
    if (p.get("capture") or {}).get("device_scale_factor", 1) != 1:
        bits.append("2x resolution")
    if (p.get("content") or {}).get("cover"):
        bits.append("cover page")
    return " · ".join(bits)


def _from_profiles() -> list:
    """Profile-backed types. Never raises — a broken registry must not stop the
    built-in reports from being submitted."""
    try:
        import registry
    except Exception as e:                              # pragma: no cover
        print(f"[report-types] profile registry unavailable: {e}", flush=True)
        return []

    out = []
    builtin_slugs = {r.slug for r in _BUILTINS}
    try:
        slugs = registry.available()
    except Exception as e:
        print(f"[report-types] could not list profiles: {e}", flush=True)
        return []

    for slug in slugs:
        if slug in builtin_slugs:
            continue          # expressed as a profile for parity, not routed
        try:
            p = registry.load(slug)
        except Exception as e:
            print(f"[report-types] skipping profile {slug}: {e}", flush=True)
            continue
        cap = p["capture"]
        out.append(ReportType(
            slug=slug, label=p["label"],
            argv=("profiles/run_profile.py", "--profile", slug),
            worker_pool=("influencer" if cap["engine"] == "influencer"
                         else "capture"),
            # A profile that pins its own worker count is not negotiable from
            # the form; one that does not may use the picker.
            allows_worker_choice=(cap.get("workers") is None
                                  and cap["engine"] != "influencer"),
            # keep_engagement is part of the profile's definition, so offering
            # it on the form would promise a choice that is not one.
            allows_keep_engagement=False,
            description=p.get("description", ""),
            caption=_caption(p),
            outputs=tuple(p.get("outputs") or ("pdf",)),
            custom=registry.is_user(slug),
            template=bool(p.get("template")),
            # Profiles may name their platform; registry.validate() has already
            # checked it against this module's table, so an unknown value can
            # never reach here.
            platform=p.get("platform", DEFAULT_PLATFORM)))
    return out


def all_types() -> list:
    """Every selectable type: built-ins first, then profiles alphabetically."""
    return list(_BUILTINS) + sorted(_from_profiles(), key=lambda r: r.label)


def get(slug: str):
    """The ReportType for `slug`, or None. Unknown slugs are rejected at the
    API boundary rather than silently falling through to a default."""
    for rt in all_types():
        if rt.slug == slug:
            return rt
    return None


def slugs() -> tuple:
    return tuple(rt.slug for rt in all_types())


def is_known(slug: str) -> bool:
    return get(slug) is not None


def check_outputs(type_slug: str, asked) -> str:
    """'' when every requested format is one this style builds, else why not.

    Same shape as `check_runnable`, and for the same reason: the tick boxes on
    the form are drawn from `rt.outputs`, but a hand-crafted POST can name
    anything. A format this style does not build must be refused here rather
    than silently ignored, or the user would be told they asked for a deck and
    handed a PDF with no explanation.
    """
    rt = get(type_slug)
    if rt is None:
        return f"Unknown report type {type_slug!r}."
    bad = [o for o in (asked or []) if o not in rt.outputs]
    if bad:
        return (f"{rt.label} does not produce "
                f"{', '.join(sorted(b.upper() for b in bad))}. It produces "
                f"{', '.join(o.upper() for o in rt.outputs)}.")
    return ""


def clean_outputs(type_slug: str, asked) -> tuple:
    """The formats to build, in the style's own order. Empty / unknown request
    means every format the style declares — what every caller before 2.4.0
    meant, and what a preset saved then still means."""
    rt = get(type_slug)
    if rt is None:
        return ()
    want = {str(o).strip().lower() for o in (asked or [])}
    kept = tuple(o for o in rt.outputs if o in want)
    return kept or tuple(rt.outputs)


# --------------------------------------------------------------------------- #
# Platforms
# --------------------------------------------------------------------------- #
def platform(slug: str):
    """The Platform for `slug`, or None."""
    for p in PLATFORMS:
        if p.slug == slug:
            return p
    return None


def platform_slugs() -> tuple:
    return tuple(p.slug for p in PLATFORMS)


def live_platforms() -> tuple:
    """Platforms a job can actually run on today."""
    return tuple(p for p in PLATFORMS if p.live and not p.combines)


def is_live(slug: str) -> bool:
    p = platform(slug)
    return bool(p and p.live)


def types_for(platform_slug: str) -> list:
    """Every style available under one platform, in `all_types()` order.

    The Style step reads this, so a style whose platform has no engine is never
    offered — the gate is the same data the pills are drawn from.
    """
    return [rt for rt in all_types() if rt.platform == platform_slug]


def check_runnable(platform_slug: str, type_slug: str) -> str:
    """'' when this pairing can run, else why not — for the API boundary.

    Disabling a pill in HTML is a hint, not a gate: a hand-crafted POST naming
    `platform=facebook` must be refused here, or the job would be created and
    then fail deep in a runner with an unrecognisable error.
    """
    p = platform(platform_slug)
    if p is None:
        return (f"Unknown platform {platform_slug!r}. Known: "
                f"{', '.join(platform_slugs())}")
    if p.combines and len(live_platforms()) < 2:
        return (f"{p.label} reports need at least two capture engines and "
                f"there is {len(live_platforms())}.")
    if not p.live:
        return f"{p.label} reports are not available yet — {p.note}"

    rt = get(type_slug)
    if rt is None:
        return f"Unknown report type {type_slug!r}."
    if rt.platform != platform_slug:
        return (f"{rt.label} is a {rt.platform} report and cannot run under "
                f"{p.label}.")
    return ""

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
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config

_PROFILES_DIR = config.ROOT / "profiles"
if str(_PROFILES_DIR) not in sys.path:
    sys.path.insert(0, str(_PROFILES_DIR))


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
    builtin: bool = False
    outputs: tuple = field(default=("pdf", "docx"))

    def default_workers(self) -> int:
        return (config.INFLUENCER_WORKERS if self.worker_pool == "influencer"
                else config.CAPTURE_WORKERS)


_BUILTINS = (
    ReportType(
        slug="twitter", label="Twitter Report",
        argv=("run.py",), worker_pool="capture",
        allows_worker_choice=True, allows_keep_engagement=True, builtin=True,
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
        description="Keeps likes & reposts in the screenshot and adds a metrics "
                    "table: Reactions, Comments, Reach, Shares.",
        outputs=("pdf", "docx")),
)


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
            outputs=tuple(p.get("outputs") or ("pdf",))))
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

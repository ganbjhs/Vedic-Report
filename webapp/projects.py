"""Projects (v3): the spine every page hangs off.

A project is a client or a recurring report. It owns the styles that print it
(picked from the style pool, one or more), and every job belongs to exactly one
project. The dropdown in the left bar picks the *current* project, which is
remembered in the signed-in session; every project page reads it from here, so
there is one answer to "which project am I looking at" per request.

Projects are shared by the whole team. `owner` is a record of who created it,
not an access gate — this is an internal tool for one team, and a report made
by a colleague in the same project is meant to be found by the next one.
"""
import re
import shutil

from fastapi import Request

from . import report_types
from .jobs import store

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
SESSION_KEY = "project_id"

# Per-project defaults kept in `projects.settings` (JSON). Everything optional;
# anything unknown is dropped on save so a stray key can never confuse a form.
SETTING_KEYS = ("dedupe", "keep_engagement", "fetch_metrics", "fast_capture",
                "workers", "report_name_pattern", "note")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or "project"


def unique_slug(name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while store.project_by_slug(slug) is not None:
        slug = f"{base[:36]}-{n}"
        n += 1
    return slug


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.match(slug or ""))


def clean_settings(raw: dict) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for k in SETTING_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if k in ("dedupe", "keep_engagement", "fetch_metrics", "fast_capture"):
            out[k] = bool(v)
        elif k == "workers":
            try:
                out[k] = max(0, int(v or 0))
            except (TypeError, ValueError):
                out[k] = 0
        else:
            out[k] = str(v or "")[:200]
    return out


# --------------------------------------------------------------------------- #
# Which project is current
# --------------------------------------------------------------------------- #
def all_projects() -> list:
    return store.projects_list()


def current(request: Request) -> dict:
    """The project the session is looking at. Falls back to the first real
    project, then to Unsorted — there is always one, `store.init` made it."""
    pid = None
    try:
        pid = request.session.get(SESSION_KEY)
    except AssertionError:                     # no SessionMiddleware (tests)
        pid = None
    proj = store.project_get(pid) if pid else None
    if proj and not proj["archived"]:
        return proj
    projects = store.projects_list()
    for p in projects:
        if p["slug"] != store.UNSORTED_SLUG:
            return p
    return projects[0] if projects else store.project_by_slug(store.UNSORTED_SLUG)


def select(request: Request, pid: str) -> dict:
    proj = store.project_get(pid)
    if proj is None:
        raise KeyError(pid)
    request.session[SESSION_KEY] = pid
    return proj


# --------------------------------------------------------------------------- #
# The project's styles, joined with what the pool knows about each one
# --------------------------------------------------------------------------- #
def styles_of(project: dict) -> list:
    """[{slug, label, outputs (chosen), all_outputs, platform, rt, missing}]

    A style that was deleted from the pool after being picked is kept in the
    list flagged `missing`, so the Styles page can say so instead of silently
    shrinking the selection.
    """
    out = []
    for item in store.project_styles(project["id"]):
        rt = report_types.get(item["slug"])
        if rt is None:
            out.append({"slug": item["slug"], "label": item["slug"],
                        "outputs": [], "all_outputs": [], "platform": "",
                        "rt": None, "missing": True})
            continue
        chosen = list(report_types.clean_outputs(rt.slug, item.get("outputs") or ()))
        out.append({"slug": rt.slug, "label": rt.label, "outputs": chosen,
                    "all_outputs": list(rt.outputs), "platform": rt.platform,
                    "caption": rt.caption, "template": rt.template,
                    "custom": rt.custom, "builtin": rt.builtin,
                    "background": _background_of(rt.slug),
                    "rt": rt, "missing": False})
    return out


def _background_of(slug: str) -> dict:
    """{color, image} of a profile's page background (empty for built-ins)."""
    try:
        import registry
        p = registry.load(slug)
    except Exception:
        return {}
    bg = (p.get("page") or {}).get("background") or {}
    return {"color": bg.get("color"), "image": bool(bg.get("image"))} if bg else {}


def runnable_styles(project: dict) -> list:
    """The project's styles that can actually run today (exist + live platform)."""
    return [s for s in styles_of(project)
            if not s["missing"] and report_types.is_live(s["platform"])]


def public(project: dict, with_styles: bool = True) -> dict:
    d = {"id": project["id"], "slug": project["slug"], "name": project["name"],
         "client": project.get("client") or "", "emoji": project.get("emoji") or "",
         "settings": project.get("settings") or {}, "archived": project["archived"],
         "created_at": project.get("created_at"),
         "is_unsorted": project["slug"] == store.UNSORTED_SLUG,
         "job_count": store.count_for_project(project["id"]),
         "style_count": len(store.project_styles(project["id"])),
         "source_count": len(store.sources_for(project["id"]))}
    if with_styles:
        d["styles"] = [{k: v for k, v in s.items() if k != "rt"}
                       for s in styles_of(project)]
    return d


# --------------------------------------------------------------------------- #
# Deleting a project (v3): what goes, what stays, and doing it
# --------------------------------------------------------------------------- #
def _fork_suffixes(project: dict) -> tuple:
    """The two slug endings `styles.fork_for_project` / `replace_page_art`
    give a style copied for this project. A custom style with one of these
    endings was made for this project and nobody else."""
    return (f"-{project['slug'][:14]}".rstrip("-"), f"-{project['id'][:8]}")


def _dir_bytes(path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def delete_preview(project: dict) -> dict:
    """Dry run. Everything `delete_project` would remove, and everything it
    would deliberately leave alone, so the confirm dialog can show both lists
    before anyone types the name.

    Deleted: the project's runs (rows + files), its sources, its style picks,
    and styles that were forked just for it and no other project uses.
    Kept:    shipped/built-in styles (they live in the code tree), custom
             styles another project also uses, and custom styles that were
             designed in the pool rather than forked for this project.
    """
    from .jobs import runner
    pid = project["id"]
    job_ids = store.project_job_ids(pid)
    run_bytes = sum(_dir_bytes(runner.job_dir(j)) for j in job_ids)
    styles_deleted, styles_kept = [], []
    suffixes = _fork_suffixes(project)
    for s in styles_of(project):
        row = {"slug": s["slug"], "label": s["label"]}
        if s["missing"]:
            continue                                  # nothing on disk to keep or lose
        if not s["custom"]:
            row["why"] = "built in" if s["builtin"] else "shipped with the app"
            styles_kept.append(row); continue
        others = [x for x in store.projects_using_style(s["slug"]) if x != pid]
        if others:
            names = [n["name"] for n in (store.project_get(o) for o in others) if n]
            row["why"] = "also used by " + (", ".join(names[:3]) + ("…" if len(names) > 3 else "")
                                             if names else "another project")
            styles_kept.append(row); continue
        if not s["slug"].endswith(suffixes):
            row["why"] = "designed in the style pool, not made for this project"
            styles_kept.append(row); continue
        styles_deleted.append(row)
    active = store.project_active_jobs(pid)
    blocked = ""
    if project["slug"] == store.UNSORTED_SLUG:
        blocked = "The Unsorted project holds the v2 reports and cannot be deleted."
    elif active:
        blocked = (f"{active} run(s) are still queued or running. Wait for them to "
                   "finish, or stop them from the Runs page, then try again.")
    return {"id": pid, "name": project["name"],
            "runs": {"count": len(job_ids), "bytes": run_bytes},
            "sources": len(store.sources_for(pid)),
            "styles_deleted": styles_deleted, "styles_kept": styles_kept,
            "active_runs": active, "blocked": blocked}


def delete_project(project: dict) -> dict:
    """Do it. Refuses (ValueError) when the preview says it is blocked; the
    caller has already checked the typed name. Files first, rows second, so
    a crash half-way leaves a project whose runs simply look empty — never a
    dangling run pointing at a project that is gone."""
    from . import styles as style_files
    from .jobs import runner
    preview = delete_preview(project)
    if preview["blocked"]:
        raise ValueError(preview["blocked"])
    pid = project["id"]
    for jid in store.project_job_ids(pid):
        shutil.rmtree(runner.job_dir(jid), ignore_errors=True)
    removed = store.project_purge(pid)
    styles_gone = []
    for row in preview["styles_deleted"]:
        try:
            if style_files.delete(row["slug"]):
                styles_gone.append(row["slug"])
        except style_files.StyleError as e:          # rule 17: say so, keep going
            print(f"[projects] kept style {row['slug']}: {e}", flush=True)
    return {"name": project["name"], "runs": removed["jobs"],
            "sources": removed["sources"], "styles": styles_gone}

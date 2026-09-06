"""Start a run — the one place jobs are created (v3).

Used by the New run page (`routes_jobs.submit_job`) and by the sheet sync loop
(`sources.py`) when a new date appears, so both paths create exactly the same
jobs: one per style, all under the project, all with the project's format
choices, all in the queue.
"""
import asyncio
import json

from . import config, projects, report_types, uploads
from .jobs import queue, runner, store


class RunError(ValueError):
    pass


def create_run(project: dict, rows: list, raw: bytes, upload_name: str,
               report_name: str, types: list = None, outputs=None,
               keep_engagement: bool = False, workers: int = 0,
               user: str = "auto", note: str = "", notes: list = None,
               fetch_metrics: bool = False, fast_capture: bool = False,
               sheet_date: str = "") -> list:
    """Create + queue one job per style. Returns the job ids.

    `types` — style slugs; None means every runnable style of the project.
    `outputs` — the union of formats asked for; each job keeps the ones its
    style builds (or everything it builds when none apply).

    `fetch_metrics` — read each X post's likes / reposts / replies / views off
    the post itself before the document is built, and fill in the sheet's metric
    columns that were left blank. Only X and combined styles can use it; for
    anything else the flag is dropped here rather than carried into a job that
    could never act on it.
    """
    if not rows:
        raise RunError("No links to run.")
    if config.MAX_LINKS and len(rows) > config.MAX_LINKS:
        raise RunError(f"{len(rows)} links — the limit is {config.MAX_LINKS} per job.")
    styles = projects.styles_of(project)
    by_slug = {s["slug"]: s for s in styles if not s["missing"]}
    if types is None:
        types = [s["slug"] for s in projects.runnable_styles(project)]
    types = list(dict.fromkeys(str(t) for t in types if str(t)))
    if not types:
        raise RunError("This project has no styles yet — pick some on the Styles page.")
    if len(types) > 6:
        raise RunError("At most 6 styles per run.")
    for t in types:
        if report_types.get(t) is None:
            raise RunError(f"Unknown style {t!r}.")

    stem = uploads.safe_stem(report_name, "Report")
    title = uploads.display_title(report_name, "Report")
    asked = [str(o).strip().lower() for o in (outputs or []) if str(o).strip()]

    job_ids = []
    for t in types:
        rt = report_types.get(t)
        keep = rt.allows_keep_engagement and bool(keep_engagement)
        want_workers = (max(0, min(int(workers or 0), config.MAX_WORKERS))
                        if rt.allows_worker_choice else 0)
        # No explicit ask → the formats the project chose for this style.
        chosen = asked or (by_slug.get(t, {}).get("outputs") or [])
        want_outputs = report_types.clean_outputs(t, chosen)
        job_stem = stem if len(types) == 1 else uploads.safe_stem(f"{stem} {rt.label}", "Report")
        want_metrics = bool(fetch_metrics) and rt.platform in ("x", "combined")
        want_fast = bool(fast_capture) and rt.allows_fast
        job_id = store.create(owner=user, name=job_stem, title=title,
                              report_type=t, link_count=len(rows),
                              upload_name=upload_name, keep_engagement=keep,
                              workers=want_workers, outputs=want_outputs,
                              project_id=project["id"],
                              fetch_metrics=want_metrics, fast_capture=want_fast)
        if sheet_date:
            store.update(job_id, sheet_date=str(sheet_date)[:10])
        try:
            runner.build_job_dir(job_id, rows, raw, upload_name)
        except Exception as e:
            store.update(job_id, status="failed", phase="Failed",
                         error=f"Could not prepare the job folder: {e}")
            raise RunError(f"Could not prepare the job: {e}")
        store.append_activity(
            job_id, f"{note or 'Started'} — {len(rows)} link(s) from '{upload_name}' · "
                    f"project {project['name']} · style {rt.label}.")
        # What the sheet reader noticed (an unnamed number column, say) is
        # said HERE too, where the person looks when the report is missing a
        # metric — not only in a preview they may have scrolled past.
        for n in (notes or []):
            store.append_activity(job_id, f"Sheet reader: {n}", "warn")
        if config.EXECUTION_MODE == "inline":
            store.update(job_id, phase="Waiting to start")
        else:
            queue.submit(job_id)
        job_ids.append(job_id)
    return job_ids


async def create_run_async(*args, **kwargs) -> list:
    return await asyncio.to_thread(create_run, *args, **kwargs)


# --------------------------------------------------------------------------- #
# Resume — one job, carried on from where another stopped
# --------------------------------------------------------------------------- #
def resume_run(old: dict, user: str = "auto") -> str:
    """Start a new job from `old`'s screenshots. Returns the new job id.

    Deliberately NOT routed through `create_run`. That function makes one job
    per style from a fresh link list; a resume is the opposite — exactly one
    job, the same style, the same name, the same formats, continuing a specific
    piece of work. Reusing it would mean explaining "one style only" to a
    function whose whole shape is "one per style".

    The rows come from the OLD job's `rows.json`, never from re-reading the
    sheet: the sheet may have gained a date since, and a resume that quietly
    captured different links than the run it claims to continue would produce a
    report nobody could explain.
    """
    from .jobs import runner

    old_id = old["id"]
    rows_file = runner.job_dir(old_id) / "rows.json"
    if not rows_file.is_file():
        raise RunError("That job's working folder is gone, so there is nothing "
                       "left to resume from. Start a new run.")
    try:
        rows = json.loads(rows_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise RunError(f"Could not read that job's link list ({e}).")
    if not rows:
        raise RunError("That job has no links recorded.")

    rt = report_types.get(old["report_type"])
    if rt is None or not rt.allows_resume:
        raise RunError(f"{(rt.label if rt else old['report_type'])} cannot be "
                       "resumed — only styles that record a result beside each "
                       "screenshot can be, and this one does not.")

    reusable = runner.resumable_count(old_id)
    if not reusable:
        raise RunError("That job has no re-usable screenshots, so resuming it "
                       "would capture every link again. Start a new run instead.")

    # The metrics step is skipped when the earlier run already did it: the rows
    # it wrote back carry the numbers, and re-reading a thousand posts to fill
    # in blanks that are no longer blank is the expensive way to change nothing.
    already_have_metrics = any(r.get("sheet_metrics") for r in rows)

    new_id = store.create(
        owner=user, name=old["name"], title=old["title"],
        report_type=old["report_type"], link_count=len(rows),
        upload_name=old.get("upload_name") or "",
        keep_engagement=bool(old.get("keep_engagement")),
        workers=int(old.get("workers") or 0),
        outputs=list(old.get("outputs") or []),
        project_id=old.get("project_id") or "",
        fetch_metrics=bool(old.get("fetch_metrics")) and not already_have_metrics,
        fast_capture=bool(old.get("fast_capture")),
        resumed_from=old_id)

    try:
        runner.build_job_dir(
            new_id, rows,
            ("\n".join(r.get("link", "") for r in rows)).encode("utf-8"),
            old.get("upload_name") or "resumed")
    except Exception as e:
        store.update(new_id, status="failed", phase="Failed",
                     error=f"Could not prepare the resumed job: {e}")
        raise RunError(f"Could not prepare the resumed job: {e}")

    copied = runner.copy_shots_for_resume(old_id, new_id)
    store.append_activity(
        new_id, f"Resumed from job {old_id} — {copied} screenshot(s) carried "
                f"over, {len(rows)} link(s) in the report. Anything that was "
                f"not a clean capture will be taken again.")
    if bool(old.get("fetch_metrics")) and already_have_metrics:
        store.append_activity(
            new_id, "Engagement numbers were already read by the earlier run "
                    "and came across with the links, so they are not read again.")
    store.append_activity(old_id, f"Resumed as job {new_id}.")

    if config.EXECUTION_MODE == "inline":
        store.update(new_id, phase="Waiting to start")
    else:
        queue.submit(new_id)
    return new_id


async def resume_run_async(*args, **kwargs) -> str:
    return await asyncio.to_thread(resume_run, *args, **kwargs)

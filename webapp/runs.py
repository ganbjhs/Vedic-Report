"""Start a run — the one place jobs are created (v3).

Used by the New run page (`routes_jobs.submit_job`) and by the sheet sync loop
(`sources.py`) when a new date appears, so both paths create exactly the same
jobs: one per style, all under the project, all with the project's format
choices, all in the queue.
"""
import asyncio

from . import config, projects, report_types, uploads
from .jobs import queue, runner, store


class RunError(ValueError):
    pass


def create_run(project: dict, rows: list, raw: bytes, upload_name: str,
               report_name: str, types: list = None, outputs=None,
               keep_engagement: bool = False, workers: int = 0,
               user: str = "auto", note: str = "", notes: list = None) -> list:
    """Create + queue one job per style. Returns the job ids.

    `types` — style slugs; None means every runnable style of the project.
    `outputs` — the union of formats asked for; each job keeps the ones its
    style builds (or everything it builds when none apply).
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
        job_id = store.create(owner=user, name=job_stem, title=title,
                              report_type=t, link_count=len(rows),
                              upload_name=upload_name, keep_engagement=keep,
                              workers=want_workers, outputs=want_outputs,
                              project_id=project["id"])
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

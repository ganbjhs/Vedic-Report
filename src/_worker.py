"""Worker body for parallel capture.

Kept in its own importable module so it pickles cleanly under the macOS 'spawn'
start method used by ProcessPoolExecutor. Each worker owns its own Playwright
browser, so many X posts are screenshotted concurrently instead of one-by-one.

Two entry points, same body:

  * `run_chunk(chunk, ...)` — a list decided up front. Its signature has not
    moved since it was first tested; `--keep-engagement` and `--fast` ride on
    the task dict precisely so it never has to.
  * `run_queue(queue, ...)` — APPROVED EDIT 6a. The worker pulls the NEXT task
    instead of being handed a fixed share, so a browser that finishes early
    takes more work rather than idling while another still has a backlog. The
    gain is small when every browser runs at the same speed (~4% on a 1232-link
    list) and large when one does not (~31% when one browser runs 1.6x slow) —
    and on an oversubscribed box, one of them always does. The measurement is
    written up in `run_report.run_tasks`.
"""
import sys
from pathlib import Path


def _one(capture, page, t):
    """Capture one task. Never raises — a dead link must not kill the chunk."""
    try:
        res = capture.capture(page, t["capture_url"], Path(t["shot"]),
                              t["platform"], t.get("keep_engagement", False),
                              t.get("fast", False))
    except Exception as e:  # network/timeout — flag, keep going
        res = {"url": t["capture_url"], "status": f"error: {e}",
               "platform": t["platform"], "screenshot": None, "handle": ""}
    res.update({"idx": t["idx"], "category": t["category"],
                "account_name": t["account"], "post_link": t["post_link"]})
    return res


def _imports(src_path):
    sys.path.insert(0, src_path)
    from playwright.sync_api import sync_playwright
    import capture  # dispatcher
    return sync_playwright, capture


def run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path):
    sync_playwright, capture = _imports(src_path)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        kwargs = dict(ctx_kwargs)
        if storage_state:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        for t in chunk:
            results.append(_one(capture, page, t))
        browser.close()
    return results


def run_queue(queue, headless, storage_state, ctx_kwargs, src_path):
    """Pull tasks until the sentinel. One browser for the whole worker's life —
    launching Chromium costs seconds, and this is what makes the pull cheap."""
    sync_playwright, capture = _imports(src_path)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        kwargs = dict(ctx_kwargs)
        if storage_state:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        # `_one` already swallows a per-link failure, so reaching this handler
        # means the BROWSER died, not a post. Return what was captured before it
        # did: on a long list that can be hundreds of finished posts, and losing
        # them because the browser fell over at post 900 would be the expensive
        # kind of tidy.
        try:
            while True:
                try:
                    t = queue.get()
                except (EOFError, OSError):    # manager went away — stop cleanly
                    break
                if t is None:                  # one sentinel per worker
                    break
                results.append(_one(capture, page, t))
        except BaseException as e:
            print(f"[runner] a capture browser stopped after "
                  f"{len(results)} post(s): {e}", flush=True)
        try:
            browser.close()
        except Exception:
            pass
    return results

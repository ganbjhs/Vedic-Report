"""Worker body for profile capture — the parallel of `src/_worker.py`.

Kept importable and argument-only so it pickles cleanly under the 'spawn' start
method (macOS), exactly as the frozen worker does.

The engine is imported DIRECTLY here (RULEBOOK rule 18 point 2), so neither
`src/capture/__init__.py`'s dispatcher nor `src/_worker.py` needs a routing
change to add a profile. Both engines are imported **read-only**; nothing under
`src/` or `influencer/` is written to, and no module state is mutated — a
profile that wants a capture knob must have it as a parameter of `capture()`
(see docs/profile-engine.md §4.2).
"""
import sys
from pathlib import Path

_MISSING_METRICS = {"followers": "—", "reactions": "—", "comments": "—",
                    "reach": "—", "shares": "—"}


def run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path, inf_path,
              engine, keep_engagement=False, fb_path=None):
    """Capture one chunk of links with `engine` ('x' | 'influencer' | 'facebook')."""
    for p in (src_path, inf_path, fb_path):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    from playwright.sync_api import sync_playwright

    influencer = engine == "influencer"
    facebook = engine == "facebook"
    if influencer:
        import inf_capture                      # read-only
        followers_cache = {}
    elif facebook:
        import fb_capture                       # facebook/, its own engine
    else:
        from capture import x_capture           # read-only

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        kwargs = dict(ctx_kwargs)
        if storage_state:
            kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**kwargs)
        page = ctx.new_page()
        for t in chunk:
            shot = Path(t["shot"])
            try:
                if influencer:
                    res = inf_capture.capture(page, t["capture_url"], shot)
                elif facebook:
                    res = fb_capture.capture(page, t["capture_url"], shot,
                                             keep_engagement)
                else:
                    res = x_capture.capture(page, t["capture_url"], shot,
                                            keep_engagement)
            except Exception as e:     # network/timeout — flag it, keep going
                res = {"url": t["capture_url"], "status": f"error: {e}",
                       "screenshot": None, "handle": ""}
                if influencer:
                    res["metrics"] = dict(_MISSING_METRICS)
            res["platform"] = "facebook" if facebook else "x"
            res.update({"idx": t["idx"], "category": t["category"],
                        "account_name": t["account"],
                        "post_link": t["post_link"]})
            if influencer:
                # Follower count needs a profile visit, so it is cached per
                # handle for the life of this process — which is why the
                # influencer engine is pinned to one worker (rule 12).
                handle = (res.get("handle") or "").lower()
                metrics = res.setdefault("metrics", dict(_MISSING_METRICS))
                if handle:
                    if handle in followers_cache:
                        metrics["followers"] = followers_cache[handle]
                    elif metrics.get("followers") not in (None, "", "—"):
                        followers_cache[handle] = metrics["followers"]
            results.append(res)
        browser.close()
    return results

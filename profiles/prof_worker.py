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


def _fb_state(storage_state):
    """The Facebook part of a merged storage_state, or None.

    A combined run's state carries the X login too; that must not ride into
    the Facebook context (nothing there needs it, and a logged-out Facebook
    visit should look like one). Only cookies for facebook.com survive, and an
    empty result means 'no storage_state at all' to the caller."""
    if not storage_state:
        return None
    cookies = [c for c in (storage_state.get("cookies") or [])
               if "facebook.com" in (c.get("domain") or "")]
    origins = [o for o in (storage_state.get("origins") or [])
               if "facebook.com" in (o.get("origin") or "")]
    if not cookies and not origins:
        return None
    return {"cookies": cookies, "origins": origins}


def run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path, inf_path,
              engine, keep_engagement=False, fb_path=None, ig_path=None):
    """Capture one chunk of links with `engine`
    ('x' | 'influencer' | 'facebook' | 'instagram' | 'combined').

    'combined' picks the engine PER TASK from t["platform"] — the X capture for
    X links, fb_capture for Facebook, ig_capture for Instagram — so one report
    can hold all three. Every engine is imported directly and read-only."""
    for p in (src_path, inf_path, fb_path, ig_path):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    from playwright.sync_api import sync_playwright

    influencer = engine == "influencer"
    combined = engine == "combined"
    engines = {}
    if influencer:
        import inf_capture                      # read-only
        followers_cache = {}
    if engine in ("facebook", "combined"):
        import fb_capture                       # facebook/, its own engine
        engines["facebook"] = lambda page, url, shot: fb_capture.capture(page, url, shot, keep_engagement)
    if engine in ("instagram", "combined"):
        import ig_capture                       # instagram/, its own engine
        engines["instagram"] = lambda page, url, shot: ig_capture.capture(page, url, shot, keep_engagement)
    if engine in ("x", "combined"):
        from capture import x_capture           # read-only
        engines["x"] = lambda page, url, shot: x_capture.capture(page, url, shot, keep_engagement)

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
            plat = t.get("platform") or engine
            if plat not in engines and not influencer:
                plat = next(iter(engines), "x")
            # Facebook gets a NEW context for every post: no cookies, no
            # storage, no HTTP cache carried over from the previous link (or
            # from the X session in a combined run). Facebook meters
            # logged-out visitors on the cookies it hands out, and the
            # second-or-third public post in one context starts answering with
            # a /login redirect; a fresh context is a first visit every time.
            # A saved sessions/fb_state.json (rare, admin-provided) is the one
            # thing that is carried in — see `_fb_state`.
            fb_ctx = fb_page = None
            work_page = page
            if plat == "facebook" and not influencer:
                try:
                    fb_kwargs = dict(ctx_kwargs)
                    fb_state = _fb_state(storage_state)
                    if fb_state:
                        fb_kwargs["storage_state"] = fb_state
                    fb_ctx = browser.new_context(**fb_kwargs)
                    fb_page = fb_ctx.new_page()
                    work_page = fb_page
                except Exception as e:      # rule 17: say so, use the shared page
                    print(f"[worker] fresh Facebook context failed ({e}); "
                          f"using the shared one", flush=True)
                    fb_ctx = fb_page = None
            try:
                if influencer:
                    res = inf_capture.capture(page, t["capture_url"], shot)
                else:
                    res = engines[plat](work_page, t["capture_url"], shot)
            except Exception as e:     # network/timeout — flag it, keep going
                res = {"url": t["capture_url"], "status": f"error: {e}",
                       "screenshot": None, "handle": ""}
                if influencer:
                    res["metrics"] = dict(_MISSING_METRICS)
            finally:
                if fb_ctx is not None:
                    try:
                        fb_ctx.close()       # cookies, storage and cache go with it
                    except Exception:
                        pass
            res["platform"] = "x" if influencer else plat
            res.update({"idx": t["idx"], "category": t["category"],
                        "account_name": t["account"],
                        "post_link": t["post_link"]})
            # A sheet with no handle column gives every row a placeholder name
            # derived from its URL ("Facebook post", a numeric page id, "X
            # post"). The capture just read the real one off the page — the FB
            # Page name, the IG @username, the X @handle — so use it. Only for
            # rows the reader flagged: a name the user typed always wins.
            if t.get("account_auto") and (res.get("handle") or "").strip():
                res["account_name"] = res["handle"].strip()
            if t.get("sheet_metrics"):
                res["sheet_metrics"] = dict(t["sheet_metrics"])
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

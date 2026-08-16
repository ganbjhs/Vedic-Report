"""Instagram post capture — the fourth engine, in its own folder (rule 18).

Frames ONE public post (/p/, /reel/, /tv/) for a LOGGED-OUT browser. What a
logged-out visitor gets from Instagram, and what this does about it:

  * a **"Log in / Sign up" panel** on top of the post shortly after load — its
    close button is clicked; if there is none, the dialog and its backdrop are
    removed and the scroll-lock released (RULEBOOK rule 19 reasoning).
  * a cookie banner — "Decline optional cookies" / "Allow all cookies" clicked.
  * a **redirect to /accounts/login/** — reported as `status="login_wall"`.
  * "Sorry, this page isn't available." — `status="not_found"`.

Framing: the post is the first `<article>` on the page (image/reel + caption +
counts). The whole article is taken; a private/removed post never reaches
this point. `keep_engagement` is accepted for interface parity and ignored —
the like/comment row is part of the article by design, as in the Influencer
report.

Result dict matches the X engine's shape:
    {status, url, handle, screenshot, text, overlay, frame_ok, parent_lost}

Written against Instagram's desktop DOM as of Aug 2026; a saved login at
sessions/ig_state.json is used if present and never required.
"""
import re
import time
from pathlib import Path

_LOGIN_PHRASES = ("log in to instagram", "log into instagram", "sign up to see")
_GONE_PHRASES = ("sorry, this page isn't available", "page not found",
                 "this content isn't available")
_CLOSE_LABELS = ("Close", "Not now", "Not Now", "Decline optional cookies",
                 "Only allow essential cookies", "Allow all cookies")

_JS_DISMISS = r"""
() => {
  const removed = [];
  const kill = (el, why) => { if (el && el.parentNode) { removed.push(why); el.parentNode.removeChild(el); } };
  document.querySelectorAll('[role="dialog"], [role="presentation"] [role="dialog"]').forEach(d => {
    if (!d.querySelector('article')) kill(d, 'dialog');
  });
  const vw = innerWidth, vh = innerHeight;
  document.querySelectorAll('body *').forEach(e => {
    const cs = getComputedStyle(e);
    if (cs.position !== 'fixed') return;
    if (e.querySelector('article')) return;
    const r = e.getBoundingClientRect();
    if (r.width >= vw * 0.85 && r.height >= vh * 0.6) kill(e, 'backdrop');
    else if (r.width >= vw * 0.85 && r.bottom > vh - 2 && r.height < vh * 0.4 && r.top > vh * 0.5) kill(e, 'bottombar');
  });
  for (const el of [document.documentElement, document.body]) {
    el.style.overflow = 'auto'; el.style.position = 'static'; el.style.height = 'auto';
  }
  return removed;
}
"""
_JS_OVERLAY = r"""() => !!document.querySelector('[role="dialog"]:not(:has(article))')"""


def _click_labels(page, labels, timeout=700) -> bool:
    for label in labels:
        for sel in (f'[role="dialog"] [aria-label="{label}"]',
                    f'[role="dialog"] svg[aria-label="{label}"]',
                    f'button:has-text("{label}")', f'[role="button"]:has-text("{label}")'):
            try:
                loc = page.locator(sel).locator("visible=true").first
                if loc.count():
                    loc.click(timeout=timeout)
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                continue
    return False


def dismiss(page) -> dict:
    _click_labels(page, _CLOSE_LABELS)
    try:
        removed = page.evaluate(_JS_DISMISS)
    except Exception:
        removed = []
    return {"removed": removed}


def overlay_present(page) -> bool:
    try:
        return bool(page.evaluate(_JS_OVERLAY))
    except Exception:
        return False


def _body_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _wait_media(page, article, budget_ms=7000) -> None:
    deadline = time.time() + budget_ms / 1000
    while time.time() < deadline:
        try:
            pending = article.evaluate(
                "el => [...el.querySelectorAll('img')].filter(i => !i.complete || i.naturalWidth === 0).length")
        except Exception:
            pending = 0
        if not pending:
            break
        page.wait_for_timeout(250)
    page.wait_for_timeout(500)


def _handle_from(page, article, url) -> str:
    m = re.search(r"instagram\.com/([^/?#]+)/(?:p|reel|reels|tv)/", url.lower())
    if m:
        return "@" + m.group(1)
    for sel in ('header a[role="link"]', 'header a', 'a[role="link"] span'):
        try:
            t = article.locator(sel).first.inner_text(timeout=500).strip()
            if t and " " not in t:
                return "@" + t.lstrip("@")
        except Exception:
            continue
    return ""


def _screenshot_clip(page, clip, shot_path) -> None:
    view_h = page.viewport_size["height"] if page.viewport_size else 0
    if view_h and clip["y"] >= 0 and clip["y"] + clip["height"] <= view_h:
        page.screenshot(path=str(shot_path), clip=clip)
        return
    sx, sy = page.evaluate("() => [window.scrollX, window.scrollY]")
    page.screenshot(path=str(shot_path), full_page=True,
                    clip=dict(clip, x=clip["x"] + sx, y=clip["y"] + sy))


def capture(page, url: str, shot_path, keep_engagement: bool = True) -> dict:
    res = {"url": url, "status": "error", "handle": "", "screenshot": None,
           "text": "", "overlay": False, "frame_ok": True, "parent_lost": False}
    shot_path = Path(shot_path)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    if "/accounts/login" in page.url:
        res["status"] = "login_wall"
        return res
    dismiss(page)
    page.wait_for_timeout(1500)                # the panel often arrives late
    dismiss(page)
    body = _body_text(page)
    has_article = page.locator("article").count() > 0
    if not has_article and any(p in body[:3000] for p in _GONE_PHRASES):
        res["status"] = "not_found"
        return res
    if not has_article and any(p in body[:3000] for p in _LOGIN_PHRASES):
        res["status"] = "login_wall"
        return res
    article = page.locator("article").first if has_article else page.locator('[role="main"], main').first
    try:
        if not article.count():
            res["status"] = "not_found"
            return res
        article.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    _wait_media(page, article)
    dismiss(page)
    page.wait_for_timeout(300)
    box = article.bounding_box()
    if not box:
        res["status"] = "not_found"
        return res
    if box["y"] < 0 or box["y"] > 40:
        page.evaluate("dy => window.scrollBy(0, dy)", box["y"] - 8)
        page.wait_for_timeout(250)
        box = article.bounding_box() or box
    clip = {"x": box["x"], "y": box["y"], "width": box["width"],
            "height": max(60, min(box["height"], 6000))}
    shot_path.parent.mkdir(parents=True, exist_ok=True)
    _screenshot_clip(page, clip, shot_path)
    res["overlay"] = overlay_present(page)
    res["handle"] = _handle_from(page, article, url)
    try:
        res["text"] = article.inner_text(timeout=800)[:500]
    except Exception:
        pass
    res["screenshot"] = str(shot_path)
    res["status"] = "ok"
    return res

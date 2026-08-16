"""Facebook post capture — the third engine, in its own folder (RULEBOOK rule 18).

Frames ONE public post: author line, text, media, and (optionally) the
reactions / comments / shares line, with everything Facebook paints over a
logged-out visitor taken off first. Nothing under src/ or influencer/ is
imported for writing; `overlays`-style dismissal is re-implemented here for
Facebook's own layers, because its DOM is different.

What a logged-out visitor gets from Facebook, and what this does about it:

  * a **login dialog** ("Log in or sign up to see more") on top of the post,
    plus a dim backdrop and a bottom "Log in" bar — closed if it has a close
    button, otherwise removed, and the body scroll-lock released. Same one-bug
    reasoning as RULEBOOK rule 19: a dialog left on the page both lands in the
    pixels AND turns scrolling into a no-op.
  * a **cookie banner** — "Decline optional cookies" / "Allow all cookies" is
    clicked so it never sits in frame.
  * a **redirect to /login** or "You must log in to continue" — reported as
    `status="login_wall"`; not screenshotted.
  * "This content isn't available right now" — `status="not_found"`.

Framing: Facebook's permalink page renders the post as `div[role="article"]`
with the comment thread as further `role="article"` nodes below and INSIDE it
(each labelled "Comment by …"). The frame therefore starts at the post's top
and ends at its actions row (Like · Comment · Share): above it when
`keep_engagement` is False, below it when True — the same one boundary the X
report is defined by (rule 6.2). If the actions row cannot be found the whole
article is taken, and `frame_ok=False` says so.

Result dict matches the X engine's shape so `prof_runner` needs no branch:
    {status, url, handle, screenshot, text, overlay, frame_ok, parent_lost}

Written against Facebook's desktop DOM as of Aug 2026; every selector below is
one Facebook can change without notice. `scripts/probe_logged_out.py` and a
2-link run are how you find out (rule 3).
"""
import re
import time
from pathlib import Path

_ARTICLE = 'div[role="article"]'
_LOGIN_PHRASES = ("you must log in to continue", "log in to facebook",
                  "log into facebook")
_GONE_PHRASES = ("this content isn't available right now",
                 "this content isn't available at the moment",
                 "this page isn't available", "content not found")

# Buttons that close a sheet politely, in the order worth trying.
_CLOSE_LABELS = ("Close", "Not now", "Not Now", "Decline optional cookies",
                 "Only allow essential cookies", "Allow all cookies")

_JS_DISMISS = r"""
() => {
  const removed = [];
  const kill = (el, why) => { if (el && el.parentNode) { removed.push(why); el.parentNode.removeChild(el); } };
  // dialogs + their backdrops (never one that contains the post itself)
  document.querySelectorAll('[role="dialog"]').forEach(d => {
    if (!d.querySelector('div[role="article"]')) kill(d, 'dialog');
  });
  // the logged-out bottom bar / login CTA form / cookie banner
  document.querySelectorAll('#login_popup_cta_form, [data-cookiebanner], [data-testid="cookie-policy-manage-dialog"]')
    .forEach(e => kill(e, 'cta'));
  // any fixed layer covering most of the viewport (dim backdrop), excluding
  // the top banner (kept narrow) and anything holding an article
  const vw = innerWidth, vh = innerHeight;
  document.querySelectorAll('body *').forEach(e => {
    const cs = getComputedStyle(e);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') return;
    if (e.querySelector('div[role="article"]')) return;
    const r = e.getBoundingClientRect();
    if (r.width >= vw * 0.85 && r.height >= vh * 0.6) kill(e, 'backdrop');
    else if (r.width >= vw * 0.85 && r.bottom > vh - 2 && r.height < vh * 0.4 && r.top > vh * 0.5) kill(e, 'bottombar');
  });
  // the fixed top banner paints over the post when it is scrolled to the top
  document.querySelectorAll('[role="banner"]').forEach(b => kill(b, 'banner'));
  // release the scroll lock a modal leaves behind
  for (const el of [document.documentElement, document.body]) {
    el.style.overflow = 'auto'; el.style.position = 'static'; el.style.height = 'auto';
  }
  return removed;
}
"""

_JS_OVERLAY_PRESENT = r"""
() => !!document.querySelector('[role="dialog"]:not(:has(div[role="article"]))')
"""


def _click_labels(page, labels, timeout=700) -> bool:
    for label in labels:
        for sel in (f'[role="dialog"] [aria-label="{label}"]',
                    f'[role="button"][aria-label="{label}"]',
                    f'button:has-text("{label}")',
                    f'[role="button"]:has-text("{label}")'):
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
    """Close, then remove, Facebook's logged-out layers; unlock scrolling."""
    _click_labels(page, _CLOSE_LABELS)
    try:
        removed = page.evaluate(_JS_DISMISS)
    except Exception:
        removed = []
    return {"removed": removed}


def overlay_present(page) -> bool:
    try:
        return bool(page.evaluate(_JS_OVERLAY_PRESENT))
    except Exception:
        return False


def _body_text(page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _expand_see_more(page, article) -> None:
    for sel in ('div[role="button"]:has-text("See more")',
                'div[role="button"]:has-text("See More")'):
        try:
            btns = article.locator(sel)
            for i in range(min(btns.count(), 3)):
                btns.nth(i).click(timeout=600)
                page.wait_for_timeout(250)
        except Exception:
            pass


def _wait_media(page, article, budget_ms=6000) -> None:
    """Give images/video posters time to arrive; poll, do not sleep blindly."""
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
    page.wait_for_timeout(400)


def _find_post(page):
    """The post's own article: the first visible role=article that is not a
    comment and is tall enough to be a post."""
    arts = page.locator(_ARTICLE)
    n = min(arts.count(), 12)
    for i in range(n):
        a = arts.nth(i)
        try:
            label = (a.get_attribute("aria-label") or "").lower()
            if label.startswith("comment"):
                continue
            box = a.bounding_box()
            if box and box["height"] >= 120 and box["width"] >= 300:
                return a
        except Exception:
            continue
    # photo viewer / video pages have no article: fall back to main
    for sel in ('[role="main"]', "main", "#content"):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.bounding_box():
                return loc
        except Exception:
            pass
    return None


def _actions_bar_box(article):
    """Bounding box of the Like · Comment · Share row inside the post, or None.
    Comments below the post carry their own Like buttons, so only a button in
    the first ~2/3 of the article that spans most of its width counts."""
    try:
        abox = article.bounding_box()
        btn = article.locator('[role="button"][aria-label="Like"], [aria-label="Like"]')
        for i in range(min(btn.count(), 4)):
            b = btn.nth(i)
            box = b.bounding_box()
            if not box or not abox:
                continue
            # climb to the row that spans the article width
            row = b.evaluate("""el => {
              let e = el, best = null;
              for (let k = 0; k < 8 && e; k++, e = e.parentElement) {
                const r = e.getBoundingClientRect();
                if (r.height < 80 && r.width > 0) best = {x:r.x, y:r.y, width:r.width, height:r.height, w:r.width};
                if (r.width >= %d * 0.85 && r.height < 80) return {x:r.x, y:r.y, width:r.width, height:r.height};
              }
              return best;
            }""" % int(abox["width"]))
            if row and row["width"] >= abox["width"] * 0.7:
                return row
    except Exception:
        pass
    return None


def _handle_from(page, article, url) -> str:
    m = re.search(r"facebook\.com/([^/?#]+)/(?:posts|photos|videos)/", url.lower())
    if m and m.group(1) not in ("photo", "watch", "reel", "share", "groups"):
        return m.group(1)
    for sel in ('h2 strong', 'h3 strong', 'strong a[role="link"]', 'a[role="link"] strong'):
        try:
            t = article.locator(sel).first.inner_text(timeout=500).strip()
            if t:
                return t[:60]
        except Exception:
            continue
    return ""


def _screenshot_clip(page, clip, shot_path) -> None:
    """Viewport-coordinate clip → file, going through document coordinates
    when the frame is taller than the viewport (same trick as the X engine)."""
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
    page.wait_for_timeout(2500)

    if re.search(r"facebook\.com/(login|checkpoint|recover)", page.url):
        res["status"] = "login_wall"
        return res
    dismiss(page)
    page.wait_for_timeout(600)
    body = _body_text(page)
    if any(p in body[:3000] for p in _LOGIN_PHRASES) and not page.locator(_ARTICLE).count():
        res["status"] = "login_wall"
        return res
    if any(p in body[:3000] for p in _GONE_PHRASES) and not page.locator(_ARTICLE).count():
        res["status"] = "not_found"
        return res

    article = _find_post(page)
    if article is None:
        res["status"] = "not_found"
        return res
    try:
        article.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    _expand_see_more(page, article)
    _wait_media(page, article)
    dismiss(page)                                   # media settling can bring a sheet back
    page.wait_for_timeout(300)

    box = article.bounding_box()
    if not box:
        res["status"] = "not_found"
        return res
    # scroll so the article's top is at the top of the viewport
    if box["y"] < 0 or box["y"] > 40:
        page.evaluate("dy => window.scrollBy(0, dy)", box["y"] - 8)
        page.wait_for_timeout(250)
        box = article.bounding_box() or box

    bar = _actions_bar_box(article)
    clip = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
    if bar:
        bottom = bar["y"] + bar["height"] + 4 if keep_engagement else bar["y"] - 4
        if bottom > clip["y"] + 60:
            clip["height"] = bottom - clip["y"]
    else:
        res["frame_ok"] = False                     # whole article, comments and all
    clip["height"] = max(60, min(clip["height"], 6000))

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

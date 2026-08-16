"""Instagram post capture — the fourth engine, in its own folder (rule 18).

Frames ONE public post (/p/, /reel/, /tv/) for a LOGGED-OUT browser. What a
logged-out visitor gets from Instagram, and what this does about it:

  * a **"Log in / Sign up" panel** on top of the post shortly after load — its
    close button is clicked; if there is none, the dialog and its backdrop are
    removed and the scroll-lock released (RULEBOOK rule 19 reasoning).
  * a cookie banner — "Decline optional cookies" / "Allow all cookies" clicked.
  * a **redirect to /accounts/login/** — reported as `status="login_wall"`.
  * "Sorry, this page isn't available." — `status="not_found"`.

Framing: a logged-out permalink has NO `<article>` — no `<ul>`, `<header>` or
`<form>` either; Instagram builds the card out of anonymous `<div>`s. The card
is found from the media outwards (`_JS_FIND_CARD`): the first ancestor much
wider than the media and no taller is the two-column card. The comment thread
and the "Log in to like or comment" row sit in the right column BESIDE the
media, so no vertical clip can remove them — they are hidden first
(`_JS_TRIM_POST`) and the card is then taken whole. `keep_engagement` is
accepted for interface parity and ignored — the like count is part of the card
by design, as in the Influencer report.

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

# --- finding the post ------------------------------------------------------
#
# A logged-out permalink has NO <article>, no <ul>, no <header> and no <form> —
# Instagram builds the whole card out of anonymous <div>s. The old rules were
# written for the logged-in DOM and matched nothing at all, so the engine fell
# back to <main> and screenshotted the navigation bar, the comment thread and
# the "More posts from …" grid along with the post.
#
# The card is found from the one element that is certainly the post: its media.
# Climbing from there, the first ancestor MUCH wider than the media and no
# taller is the two-column card (media | header+caption+comments+counts).
# It is tagged so Playwright can address it as an ordinary locator.
_JS_FIND_CARD = r"""
() => {
  document.querySelectorAll('[data-cap-post]').forEach(e => e.removeAttribute('data-cap-post'));
  const scope = document.querySelector('main') || document.body;
  const media = scope.querySelector('video')
             || [...scope.querySelectorAll('img')].find(i => i.getBoundingClientRect().width > 200);
  if (!media) return null;
  const m = media.getBoundingClientRect();
  const vw = innerWidth;
  let e = media.parentElement, card = null;
  for (let i = 0; i < 20 && e && e.tagName !== 'BODY'; i++, e = e.parentElement) {
    const r = e.getBoundingClientRect();
    if (r.width >= m.width * 1.25 && r.width <= vw * 0.95 && r.height <= m.height * 1.6) { card = e; break; }
    if (r.width > vw * 0.95) break;                       // gone past the card into the page
  }
  card = card || media.closest('article') || media.parentElement;
  card.setAttribute('data-cap-post', '1');
  const r = card.getBoundingClientRect();
  return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
          twoColumn: r.width >= m.width * 1.25};
}
"""

# The post = header (avatar + username), media, caption, and the actions row
# carrying the like count. NOT the comment thread and NOT the "Add a comment" /
# "Log in to like or comment" row. On desktop those sit in the right column
# BESIDE the media, so no vertical clip can remove them — they are hidden.
#
# Structure, not text (Instagram localises its labels the way Facebook does):
# the right column's comment area is the one scrollable box in the card
# (`overflow-y: auto`), and its children are always
# [caption, "load more" control, comments container]. The caption is kept and
# everything after it hidden.
#
# The caption is recognised by the account it is signed with, tested against
# EVERY username link in the card, not just the first: on a collab post the
# header lists the collaborators ahead of the owner, so matching only the first
# link hid the real caption and shipped a post with no text.
_JS_TRIM_POST = r"""
(root) => {
  const hidden = [];
  const hide = (el, why) => { if (el && el.style && el.style.display !== 'none') { el.style.display = 'none'; hidden.push(why); } };
  const names = [...root.querySelectorAll('a[href^="/"]')]
    .map(a => a.getAttribute('href') || '')
    .filter(h => /^\/[A-Za-z0-9._]+\/?$/.test(h))
    .map(h => h.replace(/\//g, '').toLowerCase());
  // the scrollable comment area
  const scrollers = [...root.querySelectorAll('*')].filter(e => {
    const cs = getComputedStyle(e);
    return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
           && e.getBoundingClientRect().height > 40;
  });
  scrollers.forEach(sc => {
    let w = sc;
    while (w.children.length === 1) w = w.children[0];    // skip single-child wrappers
    const kids = [...w.children];
    if (kids.length < 2) return;
    const first = (kids[0].innerText || '').trim().toLowerCase();
    // a caption is signed by one of the post's accounts; a comment carries a
    // like/reply control, which the caption never does
    const keepFirst = names.some(n => first.startsWith(n))
      || (kids[0].querySelector('a[href^="/"]') && !kids[0].querySelector('svg[aria-label]')
          && kids[0].querySelectorAll('[role="button"]').length === 0);
    kids.slice(keepFirst ? 1 : 0).forEach(k => hide(k, 'comment'));
  });
  // the logged-out "Log in to like or comment." row, and any real composer
  root.querySelectorAll('a[href*="/accounts/login"], a[href*="/accounts/signup"]').forEach(a => {
    let e = a;
    for (let i = 0; i < 4 && e.parentElement && e.parentElement !== root; i++) {
      const r = e.parentElement.getBoundingClientRect();
      if (r.height > 80) break;
      e = e.parentElement;
    }
    hide(e, 'login_row');
  });
  root.querySelectorAll('form').forEach(f => hide(f, 'composer'));
  return hidden;
}
"""


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


def _find_card(page):
    """Tag the post card with data-cap-post and return its box, or None."""
    try:
        return page.evaluate(_JS_FIND_CARD)
    except Exception:
        return None


def _handle_from(page, article, url) -> str:
    m = re.search(r"instagram\.com/([^/?#]+)/(?:p|reel|reels|tv)/", url.lower())
    if m:
        return "@" + m.group(1)
    # inside the card the author is the first /username/ link (there is no
    # <header> on a logged-out permalink)
    try:
        h = article.evaluate("""el => {
          const a = [...el.querySelectorAll('a[href^="/"]')]
            .find(x => /^\\/[A-Za-z0-9._]+\\/?$/.test(x.getAttribute('href') || ''));
          return a ? a.getAttribute('href').replace(/\\//g, '') : '';
        }""")
        if h:
            return "@" + h
    except Exception:
        pass
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
    card = _find_card(page)
    if card is None:
        if any(p in body[:3000] for p in _GONE_PHRASES):
            res["status"] = "not_found"
        elif any(p in body[:3000] for p in _LOGIN_PHRASES):
            res["status"] = "login_wall"
        else:
            res["status"] = "not_found"
        return res
    article = page.locator("[data-cap-post]").first
    try:
        article.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    _wait_media(page, article)
    dismiss(page)
    _find_card(page)                  # the card re-mounts as media settles
    article = page.locator("[data-cap-post]").first
    try:
        res["trimmed"] = article.evaluate(_JS_TRIM_POST) or []
    except Exception:
        res["trimmed"] = []
    page.wait_for_timeout(300)
    box = article.bounding_box()
    if not box:
        res["status"] = "not_found"
        return res
    if box["y"] < 0 or box["y"] > 40:
        page.evaluate("dy => window.scrollBy(0, dy)", box["y"] - 8)
        page.wait_for_timeout(250)
        box = article.bounding_box() or box
    res["cut"] = "post_card" if card.get("twoColumn") else "media_column"
    res["frame_ok"] = bool(card.get("twoColumn"))
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

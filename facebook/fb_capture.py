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
with the comment thread as further `role="article"` nodes below and INSIDE it.
The frame starts at the post's top and ends just above the Like · Comment ·
Share BUTTONS, which keeps the counts row ("644 · 45 comments · 11 shares")
and drops the controls; with `keep_engagement` False it ends above the counts
row too — the same one boundary the X report is defined by (rule 6.2). If that
row cannot be found the frame falls back to the top of the comment thread, and
then to the whole article with `frame_ok=False`.

Nothing here matches on rendered text. Facebook serves the page in the
viewer's language — this post came back in Hindi on an `en-IN` context — so
every landmark is found by structure instead; see `_JS_TRIM_POST`.

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

# Measure the post's own rows, THEN take everything below them off the page.
#
# Nothing here matches on rendered text, and that is the whole point: Facebook
# serves the page in the viewer's language (this post came back in Hindi on an
# `en-IN` context), so "Like" / "Most relevant" / "View more comments" never
# match and the old text rules silently did nothing. Every landmark below is
# structural instead:
#
#   * the ACTIONS row = 2-3 sibling `div[role="button"]` of similar size on one
#     line, each a quarter to a half of the article's width (Like · Comment ·
#     Share). Its top is the frame's bottom edge.
#   * the METRICS row = the last full-width row that ends above the actions row
#     ("644 · 45 comments · 11 shares"). Its top is the bottom edge when
#     `keep_engagement` is False.
#   * the COMMENTS block = the highest ancestor of the first nested
#     `div[role="article"]` that still starts below the actions row. Hiding that
#     ONE node removes the sort control, every comment, the "see hidden replies"
#     links, the lazy-loading skeletons and the composer together — they are all
#     inside it, which is why hiding comment articles one by one left the
#     skeletons and the reply links behind.
#
# Returns the measurements as well as what was hidden; the caller frames from
# them.
_JS_TRIM_POST = r"""
(root) => {
  const hidden = [];
  const hide = (el, why) => { if (el && el.style && el.style.display !== 'none') { el.style.display = 'none'; hidden.push(why); } };
  const R = el => el.getBoundingClientRect();
  const aw = R(root).width, atop = R(root).y;
  const near = y => y > atop + 40;                 // never mistake the header for a row

  // --- the Like · Comment · Share button row (structure, not language) ------
  const wide = [...root.querySelectorAll('div[role="button"], span[role="button"]')]
    .map(b => ({el: b, r: R(b)}))
    .filter(o => o.r.width >= aw * 0.2 && o.r.width <= aw * 0.55
                 && o.r.height >= 18 && o.r.height <= 64 && near(o.r.y));
  let actionsTop = null, actionsBottom = null;
  for (const o of wide) {
    const row = wide.filter(p => Math.abs(p.r.y - o.r.y) <= 6);
    if (row.length >= 2) {
      const top = Math.min(...row.map(p => p.r.y));
      if (actionsTop === null || top < actionsTop) {
        actionsTop = top;
        actionsBottom = Math.max(...row.map(p => p.r.bottom));
      }
    }
  }

  // --- the reactions / comments / shares counts row -------------------------
  let countsTop = null;
  if (actionsTop !== null) {
    [...root.querySelectorAll('div')].forEach(d => {
      const r = R(d);
      if (r.width < aw * 0.85 || r.height < 8 || r.height > 48) return;
      if (!near(r.y) || r.bottom > actionsTop + 2) return;
      if (countsTop === null || r.y > countsTop) countsTop = r.y;
    });
  }

  // --- the comment thread, as one node --------------------------------------
  const first = [...root.querySelectorAll('div[role="article"]')]
    .filter(a => a !== root && R(a).height > 0)[0];
  let commentsTop = first ? R(first).y : null;
  if (first) {
    let block = first, e = first.parentElement;
    const floor = actionsBottom === null ? R(first).y : actionsBottom;
    while (e && e !== root && R(e).y >= floor - 2) { block = e; e = e.parentElement; }
    hide(block, 'comments_block');
  }
  // belt and braces: anything comment-shaped the block did not contain
  root.querySelectorAll('div[role="article"]').forEach(a => { if (a !== root) hide(a, 'comment'); });
  root.querySelectorAll('form, [role="textbox"], [contenteditable="true"]').forEach(e => {
    hide(e.closest('form') || e, 'composer');
  });
  return {hidden, actionsTop, actionsBottom, countsTop, commentsTop};
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
    """The post's own article: the first visible role=article that is not
    nested inside another one and is tall enough to be a post.

    Nesting, not the aria-label, is what separates the post from its comments —
    a comment's label reads "Sanjay Awasthi का … पर कमेंट" in Hindi and
    "Comment by …" in English, and only the second one starts with "comment"."""
    arts = page.locator(_ARTICLE)
    n = min(arts.count(), 12)
    for i in range(n):
        a = arts.nth(i)
        try:
            if a.evaluate("el => !!el.parentElement.closest('div[role=\"article\"]')"):
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


def trim_post(page, article) -> dict:
    """Hide the comment thread and report where the post's own rows are.
    Returns {hidden, actionsTop, actionsBottom, countsTop, commentsTop}."""
    empty = {"hidden": [], "actionsTop": None, "actionsBottom": None,
             "countsTop": None, "commentsTop": None}
    try:
        return article.evaluate(_JS_TRIM_POST) or empty
    except Exception:
        return empty


# The Page name, read off the post's own header row.
#
# The URL is not a source for this and never was: `/share/r/<code>` and
# `/share/p/<code>` carry no name at all, and `/61559555815073/posts/…` carries
# a numeric page id, which is what used to be printed above the screenshot. The
# name is a link in the header band at the top of the article, inside an h2/h3
# or a <strong> — the timestamp beside it is a plain link, which is why the
# emphasised candidates are tried first. Structural, so it survives Facebook
# serving the page in Hindi (RULEBOOK §18b).
_JS_PAGE_NAME = r"""
(root) => {
  const R = el => el.getBoundingClientRect();
  const top = R(root).y, aw = R(root).width;
  const text = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const usable = (t) => t && t.length <= 60 && !/^https?:/i.test(t)
                        && !/^[\d.,\s]+$/.test(t);
  const band = el => { const r = R(el); return r.y - top >= -2 && r.y - top <= 140
                                          && r.height > 0 && r.width <= aw * 0.95; };
  for (const sel of ['h2 a[role="link"]', 'h3 a[role="link"]',
                     'h2 strong', 'h3 strong',
                     'strong a[role="link"]', 'a[role="link"] strong',
                     'strong span']) {
    for (const el of root.querySelectorAll(sel)) {
      if (!band(el)) continue;
      const t = text(el);
      if (usable(t)) return t;
    }
  }
  // last resort: the first profile link in the header band
  for (const a of root.querySelectorAll('a[role="link"]')) {
    if (!band(a)) continue;
    const t = text(a);
    if (usable(t) && !/^\d+\s*[hmd]$/i.test(t)) return t;
  }
  return '';
}
"""


def _handle_from(page, article, url) -> str:
    """The Page name from the post itself, falling back to the URL's slug.

    The page ALWAYS wins over the URL here. A `/share/…` link has no name in it
    and a numeric-id permalink has an id rather than a name, and that id is what
    a report with no handle column would otherwise print above the screenshot.
    """
    try:
        name = (article.evaluate(_JS_PAGE_NAME) or "").strip()
        if name:
            return name[:60]
    except Exception:
        pass
    m = re.search(r"facebook\.com/([^/?#]+)/(?:posts|photos|videos)/", url.lower())
    if m and not m.group(1).isdigit() and \
            m.group(1) not in ("photo", "watch", "reel", "share", "groups"):
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
    # Measure the rows and hide the comment thread in one pass. The rows are
    # measured BEFORE anything is hidden, so the offsets below are the ones the
    # post actually has; everything the trim removes sits under them.
    info = trim_post(page, article)
    page.wait_for_timeout(300)
    res["trimmed"] = info["hidden"]

    box = article.bounding_box()
    if not box:
        res["status"] = "not_found"
        return res
    # scroll so the article's top is at the top of the viewport
    dy = 0.0
    if box["y"] < 0 or box["y"] > 40:
        dy = box["y"] - 8
        page.evaluate("d => window.scrollBy(0, d)", dy)
        page.wait_for_timeout(250)
        new_box = article.bounding_box()
        dy = box["y"] - new_box["y"] if new_box else dy   # what the page really moved
        box = new_box or box

    clip = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
    # the measurements were taken before that scroll; move them with the page
    edge = {k: (None if info[k] is None else info[k] - dy)
            for k in ("actionsTop", "actionsBottom", "countsTop", "commentsTop")}

    if edge["actionsTop"] is not None:
        # keep_engagement -> stop just above the Like · Comment · Share BUTTONS,
        # which keeps the counts row ("644 · 45 comments · 11 shares") and drops
        # the controls; otherwise stop above the counts row as well.
        bottom = edge["actionsTop"] - 2
        if not keep_engagement and edge["countsTop"] is not None:
            bottom = edge["countsTop"] - 2
        res["cut"] = "metrics_row" if keep_engagement else "above_metrics"
    elif edge["commentsTop"] is not None:
        bottom = edge["commentsTop"] - 4          # no button row: end at the thread
        res["cut"] = "before_comments"
    else:
        bottom = clip["y"] + clip["height"]
        res["cut"] = "article_end"
        res["frame_ok"] = bool(info["hidden"])    # trimmed -> trustworthy; nothing found -> flag it
    if bottom > clip["y"] + 60:
        clip["height"] = bottom - clip["y"]
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

"""Can this URL be screenshotted WITHOUT any account?

Opens each URL in a fresh, cookie-less Chromium (the same Playwright build the
capture uses), waits, then reports what a logged-out visitor actually gets:
the final URL (a redirect to /login is the clearest "no"), whether a login /
sign-up dialog is on top of the content, and how many `article`-like nodes are
present. A screenshot of exactly what the browser saw is saved beside it, so
you can judge with your eyes (RULEBOOK rule 3) instead of trusting a flag.

    python scripts/probe_logged_out.py https://www.facebook.com/nasa/posts/... \
                                       https://x.com/NASA/status/...

    --headed   watch it happen
    --out DIR  where screenshots go (default: reports/probe)

Read-only and outside the frozen pipeline: this launches its own browser and
never touches src/ or influencer/.
"""
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

LOGIN_WORDS = ("log in", "log into", "sign in", "sign up", "join facebook",
               "create new account", "see more on facebook", "log in to continue")


def probe(page, url: str, out: Path) -> dict:
    t0 = time.time()
    resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    text = (page.inner_text("body") or "").lower()
    dialogs = page.locator('[role="dialog"]').count()
    articles = page.locator("article, [role='article']").count()
    shot = out / (re.sub(r"[^a-z0-9]+", "_", url.lower())[:80] + ".png")
    page.screenshot(path=str(shot), full_page=False)
    return {
        "url": url,
        "final_url": page.url,
        "status": resp.status if resp else None,
        "redirected_to_login": bool(re.search(r"/(login|accounts/login|i/flow/login)", page.url)),
        "login_dialog": dialogs > 0 and any(w in text for w in LOGIN_WORDS),
        "login_words_on_page": any(w in text for w in LOGIN_WORDS),
        "articles": articles,
        "seconds": round(time.time() - t0, 1),
        "screenshot": str(shot),
    }


def main(argv) -> int:
    headed = "--headed" in argv
    out = Path("reports/probe")
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    urls = [a for a in argv[1:] if a.startswith("http")]
    if not urls:
        print(__doc__)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1280, "height": 1600},
                                  locale="en-US")            # NO storage_state
        page = ctx.new_page()
        for url in urls:
            try:
                r = probe(page, url, out)
            except Exception as e:                        # report, keep going
                print(f"\n{url}\n  ERROR {type(e).__name__}: {str(e)[:160]}")
                continue
            verdict = ("NO  — redirected to a login page" if r["redirected_to_login"]
                       else "MAYBE — content loaded but a login dialog is on top "
                            "(overlays would have to remove it)" if r["login_dialog"]
                       else "YES — page rendered with no login wall" if r["articles"]
                       else "UNCLEAR — no article found; look at the screenshot")
            print(f"\n{url}\n  -> {r['final_url']}  ({r['status']}, {r['seconds']}s)")
            print(f"  articles={r['articles']}  login dialog={r['login_dialog']}  "
                  f"login words={r['login_words_on_page']}")
            print(f"  {verdict}\n  screenshot: {r['screenshot']}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

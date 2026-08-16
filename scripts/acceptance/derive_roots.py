#!/usr/bin/env python3
"""Derive ROOT-post URLs from a list of reply URLs, for the acceptance sets.

Approved edit 5 changes what happens when a post has an ancestor. Proving the
untouched path is genuinely untouched needs posts that have NO ancestor — and
the repo's own link list is all replies, so those URLs have to come from
somewhere. The parent of a reply IS a root post, so this reads them off the
conversation page.

Costs one page load per input URL and takes no screenshots. Keep the input
short — rule 21: the capture account has a daily budget.

    .venv/bin/python scripts/acceptance/derive_roots.py replies.txt roots.txt --limit 12
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "capture"))

TWEET = 'article[data-testid="tweet"]'

# JS: the status id of the FIRST article on the page. On a reply permalink that
# is the ancestor (rule 6.1), which is the root post we are after.
_FIRST_ARTICLE_STATUS = """() => {
  const a = document.querySelector('article[data-testid="tweet"]');
  if (!a) return null;
  for (const link of a.querySelectorAll('a[href*="/status/"]')) {
    const m = link.getAttribute('href').match(/^\\/([^\\/]+)\\/status\\/(\\d+)/);
    if (m) return m[1] + '|' + m[2];
  }
  return null;
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    src = Path(args.infile)
    if not src.is_absolute():
        src = here / src
    urls = [u.strip() for u in src.read_text().splitlines() if u.strip()][:args.limit]

    state_file = ROOT / "sessions" / "x_state.json"
    storage = None
    if state_file.exists():
        import json
        d = json.loads(state_file.read_text())
        storage = {"cookies": d.get("cookies", []), "origins": d.get("origins", [])}

    from playwright.sync_api import sync_playwright
    found, seen = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 1600}, locale="en-IN",
            storage_state=storage) if storage else browser.new_context(
            viewport={"width": 1280, "height": 1600}, locale="en-IN")
        page = ctx.new_page()
        for i, url in enumerate(urls, 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector(TWEET, timeout=20000)
                page.wait_for_timeout(1200)
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(800)
                got = page.evaluate(_FIRST_ARTICLE_STATUS)
            except Exception as e:
                print(f"  {i:2d}. skip ({type(e).__name__})")
                continue
            if not got:
                print(f"  {i:2d}. no article found")
                continue
            handle, sid = got.split("|", 1)
            root = f"https://x.com/{handle}/status/{sid}"
            if sid in {s for _, s in seen}:
                print(f"  {i:2d}. duplicate parent {sid}")
                continue
            seen.add((handle, sid))
            found.append(root)
            print(f"  {i:2d}. {root}")
        browser.close()

    out = Path(args.outfile)
    if not out.is_absolute():
        out = here / out
    out.write_text("\n".join(found) + ("\n" if found else ""))
    print(f"\nwrote {len(found)} unique root URL(s) -> {out}")
    if len(found) < 5:
        print("WARNING: fewer than 5 roots — the 'roots' acceptance run wants "
              "~10 to be meaningful. Add some by hand if needed.")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())

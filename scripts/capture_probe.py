"""Run ONE engine capture on ONE link and show what it decided.

    python scripts/capture_probe.py <url> [--headed] [--keep-engagement 0|1]

Picks the engine from the URL (x / facebook / instagram), uses the same
sessions/ files a job would, saves the screenshot to reports/probe/ and prints
the result dict — status, what was hidden ("trimmed"), where the frame was cut
("cut"), frame_ok, overlay. This is the fastest way to tune a framing rule:
run it on a link that came out wrong, look at the PNG, send the JSON.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("src", "profiles", "facebook", "instagram"):
    sys.path.insert(0, str(ROOT / sub))


def main(argv):
    urls = [a for a in argv[1:] if a.startswith("http")]
    if not urls:
        print(__doc__)
        return 2
    headed = "--headed" in argv
    keep = True
    if "--keep-engagement" in argv:
        keep = argv[argv.index("--keep-engagement") + 1] not in ("0", "false", "no")
    import netlinks
    from playwright.sync_api import sync_playwright
    out = ROOT / "reports" / "probe"
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        for url in urls:
            plat = netlinks.platform_of(url)
            if plat == "x":
                from capture import x_capture as eng
                state = ROOT / "sessions" / "x_state.json"
            elif plat == "facebook":
                import fb_capture as eng
                state = ROOT / "sessions" / "fb_state.json"
            elif plat == "instagram":
                import ig_capture as eng
                state = ROOT / "sessions" / "ig_state.json"
            else:
                print(f"{url}: not an X / Facebook / Instagram link")
                continue
            kwargs = {"viewport": {"width": 1280, "height": 2000}, "locale": "en-IN"}
            if state.exists():
                d = json.loads(state.read_text())
                kwargs["storage_state"] = {"cookies": d.get("cookies", []), "origins": d.get("origins", [])}
            ctx = browser.new_context(**kwargs)
            page = ctx.new_page()
            shot = out / (re.sub(r"[^a-z0-9]+", "_", url.lower())[:70] + ".png")
            try:
                res = eng.capture(page, url, shot, keep)
            except Exception as e:
                res = {"status": f"error: {e}"}
            res = {k: v for k, v in res.items() if k != "text"}
            print(f"\n{url}\n  engine={plat}  session={'yes' if state.exists() else 'no (logged-out)'}")
            print("  " + json.dumps(res, indent=2).replace("\n", "\n  "))
            ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

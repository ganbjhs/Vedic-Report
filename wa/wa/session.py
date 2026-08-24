"""
WhatsApp Web session management.

Launches a real Chromium with a *persistent* profile directory, so you scan
the QR code exactly once. Everything runs locally on your machine.
"""
from __future__ import annotations

import time
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, BrowserContext

WA_URL = "https://web.whatsapp.com/"

# --- Selectors -----------------------------------------------------------
# WhatsApp Web changes its DOM every few months. Everything selector-related
# lives here so it can be fixed in one place. Each entry is a list of
# alternatives tried in order.
SEL = {
    # left-hand "Search or start new chat" box
    "search_box": [
        'input[data-tab="3"]',                       # 2025+ build: a plain <input>
        'input[placeholder*="Search" i]',
        'div[contenteditable="true"][data-tab="3"]',
        'div[contenteditable="true"][aria-label*="Search" i]',
        'div[contenteditable="true"][aria-placeholder*="Search" i]',
        '#side div[contenteditable="true"]',
        '#pane-side ~ * div[contenteditable="true"]',
        'div[role="textbox"][aria-label*="Search" i]',
        'div[role="textbox"][aria-placeholder*="Search" i]',
        # Lexical-based editor (2024+ WhatsApp Web): first editor that is NOT the composer
        'div[contenteditable="true"][data-lexical-editor="true"]:not(#main *)',
        'div[contenteditable="true"]:not(#main *)',
        'input[placeholder*="Search" i]',
        'input[aria-label*="Search" i]',
        '#side input',
    ],
    # message composer at the bottom of an open chat
    "composer": [
        '#main footer div[contenteditable="true"][data-tab="10"]',
        '#main footer div[contenteditable="true"]',
        '#main div[contenteditable="true"][aria-placeholder*="message" i]',
        '#main div[contenteditable="true"][aria-label*="message" i]',
        'footer div[role="textbox"]',
        'div[contenteditable="true"][data-lexical-editor="true"][aria-placeholder*="message" i]',
        '#main div[contenteditable="true"]',
    ],
    # header of the open chat (contains the chat name)
    "chat_title": [
        '#main header span[title]',
        '#main header [role="button"] span',
    ],
    # element that proves we're logged in
    "logged_in": [
        '#pane-side',
        '#side',
        'div[aria-label*="Chat list" i]',
        '[role="grid"]',
    ],
    # QR canvas / login screen
    "qr": [
        'canvas[aria-label*="Scan"]',
        '[data-ref]',
    ],
    # message bubbles
    "msg_in": ['div.message-in'],
    "msg_out": ['div.message-out'],
    "msg_any": ['#main [role="row"] [data-pre-plain-text]', 'div.message-in, div.message-out'],
    # attach (paperclip / plus) button and the file input
    "attach_btn": [
        '#main footer [data-icon="plus"]',
        '#main footer [data-icon="attach-menu-plus"]',
        '#main footer [data-icon="clip"]',
        '#main footer button[aria-label*="Attach"]',
    ],
    "file_input": [
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ],
    "send_btn": [
        '[data-icon="send"]',
        'span[data-icon="send"]',
        'button[aria-label="Send"]',
    ],
}


def ensure_browsers(log=print) -> None:
    """Download Chromium with the bundled Playwright driver if it is missing (frozen macOS builds)."""
    import subprocess
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        drv = compute_driver_executable()
        cmd = list(drv) if isinstance(drv, (list, tuple)) else [str(drv)]
        log("Downloading the browser engine (one-time, ~150 MB)…")
        subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), check=True)
        log("Browser engine ready.")
    except Exception as e:  # noqa: BLE001
        log(f"Could not download the browser engine automatically: {e}. "
            f"Run:  python -m playwright install chromium")


class WASession:
    """Context manager owning the browser + a single WhatsApp Web page."""

    def __init__(self, profile_dir: str | Path = "./wa_profile", headless: bool = False,
                 slow_mo: int = 0):
        self.profile_dir = Path(profile_dir).resolve()
        self.headless = headless
        self.slow_mo = slow_mo
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "WASession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kw = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.headless:
            # WhatsApp rejects the "HeadlessChrome" UA -> present a normal desktop Chrome
            kw["user_agent"] = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
        def _launch():
            try:
                # channel="chromium" = Chromium's *new* headless mode (full browser), far closer to headed
                return self._pw.chromium.launch_persistent_context(channel="chromium", **kw)
            except Exception:  # noqa: BLE001  (older Playwright without the channel)
                return self._pw.chromium.launch_persistent_context(**kw)
        try:
            self.ctx = _launch()
        except Exception as e:  # noqa: BLE001
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                ensure_browsers()
                self.ctx = _launch()
            else:
                raise
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.goto(WA_URL, wait_until="domcontentloaded")
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -- helpers ---------------------------------------------------------
    def find(self, key: str, timeout: int = 15000, root=None):
        """Return the first locator among SEL[key] alternatives that appears."""
        root = root or self.page
        deadline = time.time() + timeout / 1000
        last_err = None
        while time.time() < deadline:
            for css in SEL[key]:
                loc = root.locator(css).first
                try:
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception as e:  # noqa: BLE001
                    last_err = e
            time.sleep(0.25)
        raise TimeoutError(f"None of the selectors for '{key}' appeared: {SEL[key]} ({last_err})")

    def wait_logged_in(self, timeout_s: int = 180) -> None:
        """Block until the chat list is visible (scan the QR if needed)."""
        print("Waiting for WhatsApp Web to load... (scan the QR code if prompted)")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for css in SEL["logged_in"]:
                if self.page.locator(css).count():
                    print("Logged in.")
                    time.sleep(1.5)  # let chat list settle
                    self.dismiss_dialogs()   # the promo/sync modal shown right after linking
                    return
            time.sleep(1)
        raise TimeoutError("Not logged in within timeout. Run `python wa.py login` and scan the QR.")

    # -- modal overlays --------------------------------------------------
    # WhatsApp Web throws up aria-modal dialogs — the "get the desktop app"
    # promo, the post-link sync notice, "restoring your chats". They swallow
    # every click aimed at the search box, which surfaces 30 s later as
    # "<div role=dialog ...> subtree intercepts pointer events" out of
    # open_chat(). Nothing works until the dialog is gone.
    #
    # SAFETY: only ever press Escape, click an explicit close/back control, or
    # click a button whose label is on the allow-list below. Never click
    # blindly inside a WhatsApp dialog — "Log out", "Delete chat", "Exit group"
    # and "Remove" all live in dialogs too.
    SAFE_DISMISS = ("continue", "ok", "okay", "not now", "later", "no thanks",
                    "dismiss", "close", "cancel", "got it", "skip",
                    "maybe later", "done")

    def dialog_text(self) -> str:
        """First 300 chars of the open modal, or '' when there is none."""
        js = """() => {
          const d = document.querySelector('[role="dialog"][aria-modal="true"]');
          return d ? (d.innerText || '').trim().slice(0, 300) : ''; }"""
        try:
            return self.page.evaluate(js) or ""
        except Exception:  # noqa: BLE001
            return ""

    def dismiss_dialogs(self, tries: int = 4, log=print) -> bool:
        """Close any blocking modal. True if the page is clear afterwards."""
        for _ in range(tries):
            txt = self.dialog_text()
            if not txt:
                return True
            first = txt.splitlines()[0][:80] if txt.splitlines() else txt[:80]
            log(f"[wa] dismissing dialog: {first!r}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
            if not self.dialog_text():
                return True
            js = """(safe) => {
              const d = document.querySelector('[role="dialog"][aria-modal="true"]');
              if (!d) return true;
              const btns = Array.from(d.querySelectorAll('button,[role="button"]'));
              const x = btns.find(b => /^(close|back)$/i.test((b.getAttribute('aria-label')||'').trim()));
              if (x) { x.click(); return true; }
              const hit = btns.find(b => safe.includes((b.innerText||'').trim().toLowerCase()));
              if (hit) { hit.click(); return true; }
              return false; }"""
            try:
                if not self.page.evaluate(js, list(self.SAFE_DISMISS)):
                    log(f"[wa] dialog has no safe dismiss button. Full text: {txt[:200]!r}")
                    return False
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.6)
        return not self.dialog_text()

    def new_tab(self) -> Page:
        """A second tab in the same logged-in profile (used for metrics scraping)."""
        return self.ctx.new_page()

    def current_chat(self) -> str:
        """Name of the chat currently open in the WhatsApp window ('' if none).

        The header shows the chat name on the first line and (for groups) the
        participant list on the second — so we take the first visible text line,
        not span[title] (that is often the participants: "Nishant, You").
        """
        js = """() => {
          const h = document.querySelector('#main header');
          if (!h) return '';
          const lines = (h.innerText || '').split('\n').map(x => x.trim()).filter(Boolean);
          if (lines.length) return lines[0];
          const t = h.querySelector('span[title]');
          return t ? (t.getAttribute('title') || '') : '';
        }"""
        try:
            return self.page.evaluate(js) or ""
        except Exception:  # noqa: BLE001
            return ""

    def dump_editors(self) -> list[dict]:
        """List every editable/textbox element with its attributes (for fixing selectors)."""
        js = """() => Array.from(document.querySelectorAll('[contenteditable="true"], [role="textbox"], input, textarea'))
          .map(e => { const o = {tag: e.tagName.toLowerCase(), inMain: !!e.closest('#main'),
                                 visible: !!(e.offsetWidth || e.offsetHeight)};
                      for (const a of e.attributes) o[a.name] = a.value.slice(0, 80);
                      return o; })"""
        return self.page.evaluate(js)

    def screenshot(self, path: str = "debug.png") -> None:
        self.page.screenshot(path=path, full_page=False)
        print(f"Screenshot saved to {path}")

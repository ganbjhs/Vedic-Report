"""
Chat-level actions: open a chat by name, send text / files.
"""
from __future__ import annotations

import time
from pathlib import Path

from .session import WASession


_JS_CLICK_IN_LIST = r"""(name) => {
  const main = document.querySelector('#main');
  let spans = Array.from(document.querySelectorAll('#pane-side [data-testid="cell-frame-title"] span[title], #pane-side span[title]'));
  if (!spans.length) spans = Array.from(document.querySelectorAll('span[title]')).filter(e => !(main && main.contains(e)));
  let exact = spans.find(e => e.getAttribute('title') === name)
      || spans.find(e => e.getAttribute('title').toLowerCase() === name.toLowerCase())
      || spans.find(e => e.getAttribute('title').toLowerCase().includes(name.toLowerCase()));
  if (!exact) {   // newer builds: no title attr -> match visible text in the left pane
    const cands = Array.from(document.querySelectorAll('span, div'))
      .filter(e => !(main && main.contains(e)) && e.children.length === 0 && (e.textContent || '').trim());
    const n = name.trim().toLowerCase();
    exact = cands.find(e => e.textContent.trim() === name.trim())
         || cands.find(e => e.textContent.trim().toLowerCase() === n);
  }
  if (!exact) return false;
  exact.scrollIntoView({block: 'center'});
  const row = exact.closest('[role="row"], [role="listitem"], [role="gridcell"]') || exact;
  row.click();
  return true;
}"""


def _focus_editor(s: WASession, box) -> None:
    """Put the caret in the search box, whatever is floating over it.

    Three attempts, cheapest first, because only the LAST one is guaranteed:

      1. a real click — hit-tested, so it fails if anything overlaps the box;
      2. a forced click — skips the hit test (Playwright still dispatches at
         the box's own coordinates);
      3. `el.focus()` — no pointer involved at all, so nothing can intercept it.

    Typing afterwards goes wherever the caret is, so focus is all this needs to
    achieve; a click was never the point. Short timeouts on purpose: the bug
    this replaces spent Playwright's full 30 s default on a tooltip, three
    times over, before the bot reported "Could not open the group".
    """
    s.clear_overlays()
    errors = []
    for how in ("click", "force", "focus"):
        try:
            if how == "click":
                box.click(timeout=5000)
            elif how == "force":
                box.click(force=True, timeout=3000)
            else:
                box.evaluate("el => el.focus()")
            if how != "click":
                print(f"[wa] search box focused via {how} (something was covering it)", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            errors.append(f"{how}: {str(e).splitlines()[0][:90]}")
            s.clear_overlays()
    raise RuntimeError("Could not put the caret in the search box — " + "; ".join(errors))


def open_chat(s: WASession, name: str, timeout: int = 15000) -> None:
    """Open a chat (contact or group) by the exact name shown in WhatsApp."""
    if s.current_chat().strip().lower() == name.strip().lower():
        return  # already open
    # A modal swallows every click aimed at the chat list or the search box;
    # a tooltip in #wa-popovers-bucket does the same without being a dialog.
    s.dismiss_dialogs()
    s.clear_overlays()
    # The chat list may still be rendering — a headless start reaches here
    # before #pane-side exists, and then step 1 below finds no rows and the
    # search box is the only way in. Waiting a moment makes step 1 work, and
    # step 1 is the path no overlay can block.
    try:
        s.find("logged_in", min(timeout, 10000))
        time.sleep(0.4)
    except TimeoutError:
        pass
    # 1) Try clicking it directly in the chat list. This is a DOM click, not a
    #    hit-tested one, so it works even with something floating on top.
    if s.page.evaluate(_JS_CLICK_IN_LIST, name):
        time.sleep(0.8)
        if name.lower() in s.current_chat().lower():
            return
    # 2) Fall back to the search box
    box = _search_box(s, timeout)
    _focus_editor(s, box)
    s.page.keyboard.press("Meta+A" if _is_mac() else "Control+A")
    s.page.keyboard.press("Backspace")
    s.page.keyboard.type(name, delay=20)
    time.sleep(1.0)  # let results render
    # First result whose title matches exactly, then 'contains', then just press Enter
    clicked = False
    s.clear_overlays()
    for loc in (s.page.locator(f'span[title="{name}"]:not(#main *)').first,
                s.page.locator('span[title]:not(#main *)', has_text=name).first):
        try:
            loc.wait_for(state="visible", timeout=4000)
            try:
                loc.click(timeout=5000)
            except Exception:  # noqa: BLE001  something floated over the result row
                s.clear_overlays()
                loc.click(force=True, timeout=3000)
            clicked = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        s.page.keyboard.press("Enter")
    # verify header shows the chat we asked for
    title = s.find("chat_title", timeout)
    title.wait_for(state="visible", timeout=timeout)
    time.sleep(0.6)
    shown = s.current_chat().strip()
    if shown and name.lower() not in shown.lower() and shown.lower() not in name.lower():
        raise RuntimeError(f"Opened chat '{shown}' but wanted '{name}' — check the exact chat name")


def _search_box(s: WASession, timeout: int):
    """Find the chat-list search editor; on newer builds it only becomes editable after a click."""
    try:
        return s.find("search_box", min(timeout, 5000))
    except TimeoutError:
        pass
    # Click anything that looks like the search entry point, then retry
    for css in ('button[aria-label*="Search" i]', '[aria-label*="Search or start" i]',
                'div[title*="Search" i]', 'span[data-icon="search"]', '[data-icon="search-refreshed"]'):
        loc = s.page.locator(css).first
        if loc.count():
            try:
                try:
                    loc.click(timeout=2000)
                except Exception:  # noqa: BLE001
                    s.clear_overlays()
                    loc.click(force=True, timeout=2000)
                break
            except Exception:  # noqa: BLE001
                continue
    try:
        return s.find("search_box", timeout)
    except TimeoutError:
        # Last resort: keyboard shortcut for search (Cmd/Ctrl+Alt+/) then any editor
        s.page.keyboard.press(("Meta" if _is_mac() else "Control") + "+Alt+/")
        time.sleep(0.5)
        return s.find("search_box", 3000)


def send_text(s: WASession, text: str, delay_after: float = 1.0) -> None:
    """Type a (possibly multi-line) message into the open chat and send it."""
    box = s.find("composer")
    # Same hazard as the search box: a tooltip floating over the composer would
    # otherwise cost 30 s per message and then fail the whole run.
    _focus_editor(s, box)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            s.page.keyboard.type(line, delay=5)
        if i < len(lines) - 1:
            s.page.keyboard.press("Shift+Enter")  # newline without sending
    time.sleep(0.3)  # let link-preview render (optional)
    s.page.keyboard.press("Enter")
    time.sleep(delay_after)


def send_file(s: WASession, path: str | Path, caption: str = "", delay_after: float = 2.0) -> None:
    """Attach an image/video/document to the open chat."""
    path = str(Path(path).resolve())
    s.find("attach_btn").click()
    time.sleep(0.5)
    inp = s.page.locator('input[type="file"]').first
    inp.set_input_files(path)
    time.sleep(1.5)
    if caption:
        cap = s.page.locator('div[contenteditable="true"][data-tab]').last
        cap.click()
        s.page.keyboard.type(caption, delay=5)
    s.find("send_btn").click()
    time.sleep(delay_after)


def send_bulk_from_file(s: WASession, chat: str, file_path: str, text: str = "",
                        delay: float = 1.0, tail: str | None = None) -> int:
    """
    Port of your original AutomateMsgSender.send_messages_from_file():
    every non-blank line of `file_path` is sent as `line\\ntext` into `chat`.
    Returns number of messages sent.
    """
    lines = [l.strip() for l in Path(file_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        print(f"No messages found in {file_path}.")
        return 0
    open_chat(s, chat)
    for line in lines:
        full = f"{line}\n{text}" if text else line
        send_text(s, full, delay_after=delay)
    if tail:
        send_text(s, tail, delay_after=delay)
    print(f"Sent {len(lines)} messages from {file_path} to '{chat}'.")
    return len(lines)


def _is_mac() -> bool:
    import sys
    return sys.platform == "darwin"

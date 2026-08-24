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


def open_chat(s: WASession, name: str, timeout: int = 15000) -> None:
    """Open a chat (contact or group) by the exact name shown in WhatsApp."""
    if s.current_chat().strip().lower() == name.strip().lower():
        return  # already open
    # A modal swallows every click aimed at the chat list or the search box.
    s.dismiss_dialogs()
    # 1) Try clicking it directly in the chat list (works even if the search box moved)
    if s.page.evaluate(_JS_CLICK_IN_LIST, name):
        time.sleep(0.8)
        if name.lower() in s.current_chat().lower():
            return
    # 2) Fall back to the search box
    box = _search_box(s, timeout)
    box.click()
    s.page.keyboard.press("Meta+A" if _is_mac() else "Control+A")
    s.page.keyboard.press("Backspace")
    s.page.keyboard.type(name, delay=20)
    time.sleep(1.0)  # let results render
    # First result whose title matches exactly, then 'contains', then just press Enter
    clicked = False
    for loc in (s.page.locator(f'span[title="{name}"]:not(#main *)').first,
                s.page.locator('span[title]:not(#main *)', has_text=name).first):
        try:
            loc.wait_for(state="visible", timeout=4000)
            loc.click()
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
                loc.click(timeout=2000)
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
    box.click()
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

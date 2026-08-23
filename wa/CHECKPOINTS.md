# Checkpoints

Local change history of WA Toolkit (newest at the bottom). From checkpoint 001 on,
entries are written automatically by `python checkpoint.py save "<message>"`, and each
has a zip snapshot in `checkpoints/`. Entries before that are the reconstructed history
of the build-out (2026-08-17 → 2026-08-18) so nothing is lost.

## History before checkpointing existed

| # | when | change |
|---|---|---|
| H1 | 08-17 | Reviewed `AutoMessageSender/AutomateMsgSender.py` (pyautogui + clipboard). Decided: Python + Playwright driving WhatsApp Web with a persistent profile, local only. |
| H2 | 08-17 | First toolkit: `wa/session.py chat.py reader.py links.py sheets.py metrics.py`, CLI `wa.py` (login/send/collect/read/metrics/debug). Link classifier unit-tested. |
| H3 | 08-17 | PyYAML unavailable on Mac → switched to `config.json`. Sender matching by name or number (digits-suffix). Phone parsed from message id when exposed. |
| H4 | 08-17 | Task engine `wa/engine.py` (JSON recipes in `tasks.json`, `{var}` templating, plugins/), `wa.py tasks/run`. |
| H5 | 08-18 | Desktop app `app.py` (pywebview) with worker thread + job queue; first HTML UI. |
| H6 | 08-18 | Simple mode: target = whatever chat is open in the WhatsApp window (no names, no JSON). `current_chat()`. Collect: saved groups, scan senders, TSV/CSV/Sheet outputs. |
| H7 | 08-18 | Command bot `bot.py` (/start → 1|2 → lists → /run, numbering, /status /cancel /target /help), headless with desktop UA; Bot tab in app (subprocess); profile-lock guards. |
| H8 | 08-18 | UI rewritten in React (`ui-src/` → `ui/bundle.js` via esbuild). |
| H9 | 08-18 | Live DOM inspection of WhatsApp Web (2025 build): `role=row` messages, `data-pre-plain-text`, input search box, header title = members. Reader/selectors rewritten; `open_chat` clicks chat list first; header name = first text line. Group name typo found (`forword_links`). |
| H10 | 08-18 | Bot fixes: own replies were re-read (emoji dropped by innerText) → node-walk text extraction; line breaks no longer doubled; `ignore_own`; newest-message-only. |
| H11 | 08-18 | Long lists truncated → "Read more" clicked repeatedly (≈3000 chars per click). |
| H12 | 08-18 | Bot wording: short, assistant-style, no paragraphs; visible marker `🔹`. |

## 001 — packaging (PyInstaller spec, Windows/mac builds, Inno Setup, GitHub Actions), wa/paths.py for frozen builds, RULEBOOK, BLUEPRINT, checkpoint.py, README refresh
*2026-08-18 02:16* · `checkpoints/001-packaging-pyinstaller-spec-windows-mac-b.zip`

- **added:** `.github/workflows/build.yml`, `.gitignore`, `BLUEPRINT.md`, `CHECKPOINTS.md`, `README.md`, `RULEBOOK.md`, `app.py`, `bot.py`, `checkpoint.py`, `config.json`, `file1.txt`, `file2.txt`, `packaging/WAToolkit.spec`, `packaging/build_mac.sh`, `packaging/build_windows.bat`, `packaging/installer.iss`, `plugins/example.py`, `requirements.txt`, `tasks.json`, `test_links.py`, `ui-src/app.jsx`, `ui-src/build.sh`, `ui-src/package-lock.json`, `ui-src/package.json`, `ui-src/styles.css`, `ui/bundle.css`, `ui/bundle.js`, `ui/index.html`, `wa.py`, `wa/__init__.py`, `wa/chat.py`, `wa/engine.py`, `wa/links.py`, `wa/metrics.py`, `wa/paths.py`, `wa/reader.py`, `wa/session.py`, `wa/sheets.py`

## 002 — move GitHub workflow to packaging/github-build.yml (protected path on device)
*2026-08-18 02:17* · `checkpoints/002-move-github-workflow-to-packaging-github.zip`

- **added:** `packaging/github-build.yml`
- **modified:** `BLUEPRINT.md`, `CHECKPOINTS.md`, `README.md`
- **deleted:** `.github/workflows/build.yml`

## 003 — gitignore finalised, push_to_github scripts, README GitHub/Windows steps
*2026-08-18 02:23* · `checkpoints/003-gitignore-finalised-push-to-github-scrip.zip`

- **added:** `packaging/push_to_github.bat`, `packaging/push_to_github.sh`
- **modified:** `.gitignore`, `CHECKPOINTS.md`, `README.md`

## 004 — macOS build: don't bundle Chromium, download on first launch (ensure_browsers); Windows installer confirmed building on GitHub
*2026-08-18 15:52* · `checkpoints/004-macos-build-don-t-bundle-chromium-downlo.zip`

- **modified:** `CHECKPOINTS.md`, `packaging/build_mac.sh`, `packaging/github-build.yml`, `wa/paths.py`, `wa/session.py`

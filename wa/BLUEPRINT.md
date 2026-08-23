# WA Toolkit — Blueprint

The complete design of the application, written so that a person (or an AI) can
rebuild or redesign the whole thing from this file alone. `RULEBOOK.md` holds the
rules; `README.md` holds the how-to; this holds the *what and why*.

---

## 0. Purpose (one paragraph)

A **local, no-server WhatsApp automation tool for personal use**, aimed at a
non-programmer. It drives WhatsApp Web in a real Chromium (Playwright) using a
saved login, and offers three things: (1) send a list of messages into a chat,
(2) collect post/comment links from groups filtered by sender into a Google
Sheet/CSV, (3) a **command bot** — a second WhatsApp number that sits in a
control group and executes "message-list → send one by one → number the list"
jobs when the user types `/start … /run` from their phone. Everything runs on
the user's own computer; the WhatsApp login is stored once in a local profile.

## 1. Runtime topology

```
┌───────────────────────────── user's computer ─────────────────────────────┐
│  app.py (pywebview window, React UI)          bot.py (separate process)   │
│    └─ Worker thread ── Playwright ── Chromium ─┐   └─ Playwright ── Chromium (headless)
│                                               │            │               │
│                       wa_profile/  (ONE login, ONE browser at a time) ◄────┘
│  config.json  tasks.json  plugins/  links.csv  service_account.json        │
└────────────────────────────────────────────────────────────────────────────┘
          │ https                                   │ https (optional)
   web.whatsapp.com                          Google Sheets API (gspread)
```

- **No server, no database.** State = files in `DATA_DIR` (see §7).
- **One Chromium per profile.** app and bot cannot both hold the browser; the app closes its window before spawning the bot and refuses to open one while the bot runs.
- Bot = a *second WhatsApp number* (recommended) logged into `wa_profile/`; the human commands it from their main number inside a group they both belong to.

## 2. Repository layout

```
wa_toolkit/
├─ app.py                desktop app (pywebview) – Worker thread + Api exposed to JS; `--bot` flag = run bot.main()
├─ bot.py                command bot (state machine + poll loop), headless by default
├─ wa.py                 CLI (login/send/collect/read/metrics/debug/tasks/run)
├─ checkpoint.py         local version history (zip snapshots + CHECKPOINTS.md)
├─ config.json           all settings (schema §6)
├─ tasks.json            recipes for the engine (schema §6)
├─ plugins/example.py    user Python hooks callable from recipes
├─ file1.txt file2.txt   sample inputs for "send lines"
├─ wa/
│  ├─ paths.py           APP_DIR / DATA_DIR / CONFIG / TASKS / UI_INDEX (source vs frozen)
│  ├─ session.py         WASession (launch, login wait, current_chat, dump_editors, screenshot) + SEL
│  ├─ chat.py            open_chat, send_text, send_file, send_bulk_from_file
│  ├─ reader.py          read_visible, read_history (+ JS extract/expand/scroll)
│  ├─ links.py           clean_url, classify, extract_urls, PLATFORMS
│  ├─ sheets.py          SheetWriter (gspread), CsvWriter, HEADERS
│  ├─ metrics.py         fetch_metrics(platform,url,page)
│  └─ engine.py          Engine, ACTIONS, HELP, render(), sender_matches, links_from_messages
├─ ui/                   built UI: index.html + bundle.js + bundle.css (do not edit by hand)
├─ ui-src/               React source: app.jsx, styles.css, build.sh (esbuild), package.json
├─ packaging/            WAToolkit.spec (PyInstaller), build_windows.bat, build_mac.sh, installer.iss (Inno Setup)
├─ packaging/github-build.yml   GitHub Actions workflow (copy to .github/workflows/build.yml) – builds Windows installer + mac dmg
├─ README.md RULEBOOK.md BLUEPRINT.md CHECKPOINTS.md
└─ test_links.py
```

## 3. Layer contracts (the redesignable core)

Any redesign must keep these contracts; everything above them (UI, bot, CLI) is replaceable.

### 3.1 `WASession` (wa/session.py)
```
WASession(profile_dir=".\/wa_profile", headless=False, slow_mo=0)   # context manager
  .page                    Playwright Page on web.whatsapp.com
  .wait_logged_in(timeout_s)   -> None | TimeoutError      (chat list visible)
  .find(key, timeout, root)    -> Locator  (first SEL[key] alternative that is visible)
  .current_chat()          -> str  ('' if no chat open)  = first text line of #main header
  .new_tab()               -> Page (same login; used for metrics)
  .dump_editors()          -> [ {tag, attrs…} ]  (debug)
  .screenshot(path)
SEL = { search_box, composer, chat_title, logged_in, qr, msg_*, attach_btn, file_input, send_btn }  # lists of CSS alternatives
```
Headless: sets a desktop Chrome UA and tries `channel="chromium"` (new headless), falls back to default.

### 3.2 Chat actions (wa/chat.py)
```
open_chat(session, name)     click chat in list (title attr or visible text) → else search box (input[data-tab=3]) → verify current_chat()
send_text(session, text, delay_after=1.0)   types into composer; "\n" → Shift+Enter; Enter sends
send_file(session, path, caption="")
send_bulk_from_file(session, chat, file_path, text="", delay=1.0, tail=None) -> int
```

### 3.3 Message dict (wa/reader.py) — the universal data shape
```
{ id: str|None,            conv-msg id (or data-id)
  sender: str,             contact name if saved else phone; "me" for own when no name
  phone: str,              "+91…" when known ('' otherwise)
  time: datetime|None,     parsed from data-pre-plain-text  "[6:33 AM, 8/18/2026] Name: "
  text: str,               exact text (emoji kept, single "\n" per line break)
  links: [str],            hrefs + urls found in text
  outgoing: bool }
read_visible(session, expand=True) -> [msg]        (expand = click "Read more" until gone)
read_history(session, max_messages=300, since=None, days=None) -> [msg]   (scrolls up)
```

### 3.4 Links (wa/links.py) — pure
```
classify(url) -> {url, clean_url, platform, kind}   platform ∈ instagram|twitter|facebook|linkedin|youtube|threads|tiktok|reddit|other
                                                    kind ∈ post|comment|story|profile|other
PLATFORMS = [(name, host_predicate, [(kind, regex), …]), …]   # first match wins; extend here
```

### 3.5 Output rows (wa/sheets.py)
```
HEADERS = [collected_at, group, sender, phone, sent_at, platform, kind, url, clean_url, message, likes, comments, shares, views, metrics_note]
SheetWriter(sheet_id, worksheet, creds).existing_urls() / .append(rows) ; CsvWriter(path) same interface
```

### 3.6 Engine (wa/engine.py)
```
Engine(session, cfg, log_fn).run(steps, ctx)   steps = [{action, …params, save_as?}]
ACTIONS: open_chat, send_text, send_file, send_lines, read_history, filter, extract_links, metrics,
         to_sheet, to_csv, for_each(list|from, as, steps, collect), set, print, wait, python(func in plugins/)
render(): "{var}" / "{var.field}" substitution; whole-string placeholder keeps type
```

### 3.7 App API (app.py `Api`, called from JS via `window.pywebview.api.<name>()`)
```
state:   status() log_since(n) result() current_chat() bot_status()
simple:  login() close_browser() scan(days,n) send_lines(lines,text,delay) collect(optsJson) save_rows(mode) last_rows()
         pick_file() groups() set_groups(list) sheets_ready() open_folder() stop() set_keep_browser(bool)
recipes: list_tasks() get_task(n) save_task(n,json) delete_task(n) actions_help() run_task(n,varsJson)
config:  get_config() save_config(json)
bot:     bot_start(group, headed) bot_stop()
tools:   debug(chat)
```
Worker: single thread, `jobs` queue, `job_<kind>(**payload)`; `silent` jobs (current_chat) don't touch status/log.
Bot is a **subprocess** (`bot.py` from source, `WAToolkit --bot` when frozen); stdout captured into `bot_status().lines`.

### 3.8 Bot state machine (bot.py)
```
idle ──/start──► mode ──"1"|"2"──► collect ──/run──► (execute) ──► idle
collect: each non-command message = one list (split on blank lines);  mode 2: a link-only message attaches to the last list
/status /cancel /target <name> /help work in any state.  Only the newest message is considered; own/outgoing ignored (ignore_own).
execute: for i,list: for msg: send(msg [+ "\n"+link]) ; send(str(i))     → "Done." → snapshot seen
Replies are prefixed BOT="🔹 " (short, no paragraphs).
```

## 4. UI specification (ui-src/app.jsx)

Sidebar: brand, status pill (bot running / working / open chat / closed), nav
**Send messages · Collect links · Bot (from phone) · Advanced**, footer buttons
(Open/Close WhatsApp, Stop). Bottom: collapsible Activity drawer (log).

- **Send**: banner "Target: <open chat>" (warn if none) · textarea (one message per line) · Load .txt · "Add under every message" · delay · big *Send to "<chat>"* (confirm count).
- **Collect**: banner + *Save this group* · saved-group chips + "collect from all my saved groups" · days/max · Post/Comment/metrics checkboxes · *Scan open group for people* → sender chips (toggle) or typed names/numbers · *Collect links* → results table · Copy for Google Sheets (TSV) / Save CSV / Save to Google Sheet (if configured) / Open folder.
- **Bot**: control group input · show-browser checkbox · Start/Stop · state banner · details toggle/copy · numbered how-to.
- **Advanced**: tabs Recipes (chips + vars + Run) · Edit JSON (task editor + actions cheat-sheet) · Tools/debug (login, close, keep-open, Debug selectors + copy) · Config (JSON editor).
Design tokens in `styles.css` (`--brand #128c7e`, light/dark via `prefers-color-scheme`, radius 12, Inter/system font). Build: `cd ui-src && npm i && ./build.sh` → `ui/bundle.js|css`.

## 5. Flows

**Login (once)** `python wa.py login` / app *Open WhatsApp* → QR → profile saved in `wa_profile/`.
**Send (simple)** user opens chat in WhatsApp window → app reads `current_chat()` → `send_lines` job → `send_text` per line.
**Collect (simple)** `_need_chat()` → `read_history(days,max)` → filter `sender_matches` → `links_from_messages(kinds)` → dedupe → optional metrics → rows → save (Sheet/CSV/TSV).
**Bot job** phone: `/start`→`2`→list→link→…→`/run`; bot: poll every 2 s `read_visible` (expand Read more) → newest unseen non-own message → `handle()` → replies; `execute()` sends and numbers; `snapshot_seen()` so its own output is never re-read.
**Recipe** `wa.py run name --var k=v` or Advanced → Recipes: `Engine.run(steps)`.

## 6. Data schemas

**config.json**
```json
{ "profile_dir": "./wa_profile", "headless": false,
  "send":    { "chat": "", "delay": 1.5, "jobs": [ {"file": "file1.txt", "text": ""} ] },
  "collect": { "groups": [], "senders": [], "include_me": false, "days": 7, "max_messages": 400,
               "kinds": ["post","comment"], "platforms": [], "metrics": false, "metrics_delay": 4 },
  "sheets":  { "mode": "gspread|csv", "sheet_id": "", "worksheet": "links", "creds": "service_account.json", "csv_path": "links.csv" },
  "bot":     { "group": "", "headless": true, "delay": 1.5, "poll": 2.0, "target": null, "ignore_own": true } }
```
**tasks.json** `{ "<name>": { "description": str, "vars": {…}, "steps": [ {"action": str, …} ] } }`
**Sheet row** = HEADERS above. **Sender matching**: ≥7 digits → digits-suffix match; else case-insensitive contains (leading `~` ignored).

## 7. Packaging (no Python for end users)

- `wa/paths.py`: frozen → `APP_DIR = sys._MEIPASS` (bundle), `DATA_DIR = %APPDATA%\WAToolkit` / `~/Library/Application Support/WAToolkit`; first run copies default config/tasks/plugins there; sets `PLAYWRIGHT_BROWSERS_PATH=0` (browsers bundled inside the playwright package).
- Build: `PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium` then `pyinstaller packaging/WAToolkit.spec` (collect_all playwright + webview, ui/, wa/, plugins/, defaults). One executable `WAToolkit(.exe)`; bot mode = `WAToolkit --bot --group X` (app.py dispatch).
- Windows installer: `packaging/installer.iss` (Inno Setup) → `WAToolkit-Setup.exe`. macOS: `.app` + `.dmg`.
- Zero-Windows-machine path: push to GitHub → Actions "Build installers" → download artifacts.
- Windows needs Edge WebView2 (present on Win10/11) for pywebview; Chromium (~150 MB) is inside the package; nothing else.

## 8. Known limitations / open ends

- WhatsApp Web DOM changes → selectors break; fix in one place (RULEBOOK §2.2-3), Debug selectors tool helps.
- Headless works with saved login + desktop UA; if WhatsApp blocks it, run bot headed.
- Metrics scraping is best-effort; reach is impossible for others' posts.
- `read_history` covers what WhatsApp renders/loads while scrolling (virtualised list) — very old history may need more scrolls (`max_scrolls`).
- Bot ids: new WhatsApp uses opaque message ids; phone numbers only when the bubble label is a number.

## 9. Redesign guide (how to change big things safely)

| Want to… | Change | Keep |
|---|---|---|
| New UI framework / look | `ui-src/` (rebuild bundle) | Api contract §3.7 |
| Different desktop shell (Electron, Tauri) | replace `app.py` window code, talk to the same Worker via HTTP/IPC | Worker + jobs, contracts §3.1-3.6 |
| Support another messenger | new `session/chat/reader` implementing §3.1-3.3 | links/sheets/engine/bot untouched |
| New bot commands / flow | `bot.py handle()` + docs | `ignore_own`, newest-only, marker prefix, snapshot after execute |
| New link platform | `PLATFORMS` in `wa/links.py` + `test_links.py` | classify() shape |
| New output (Notion, Excel…) | new writer with `existing_urls()/append()` | HEADERS |
| Node.js instead of Python | port §3 contracts; whatsapp-web.js gives §3.1-3.3 for free | schemas §6, bot state machine §3.8 |

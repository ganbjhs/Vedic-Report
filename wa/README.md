# WA Toolkit — personal WhatsApp automation (local, no server)

Drives WhatsApp Web in a real Chromium with a saved login and gives you:

* **Send messages** – paste a list, it goes into the chat you have open.
* **Collect links** – post/comment links from groups, filtered by sender → Google Sheet / CSV.
* **Command bot** – a second WhatsApp number that runs "message-list → send one by one → number the list" jobs when you type `/start … /run` from your phone. Headless, no window.
* Desktop app (React UI) for non-programmers, CLI + JSON recipes for power users, installers for Windows/macOS.

Docs: `README.md` (this – how to use) · `BLUEPRINT.md` (how it is built, redesign guide) · `RULEBOOK.md` (roles + strict rules) · `CHECKPOINTS.md` (change history).

## Quick start (end user, installer)

Windows: run `WAToolkit-Setup.exe` → launch *WA Toolkit* → *Open WhatsApp* → scan the QR once.
macOS: open `WAToolkit.dmg` → drag *WA Toolkit* to Applications → same.
Nothing else to install (Chromium is inside the app). Your data lives in `%APPDATA%\WAToolkit` / `~/Library/Application Support/WAToolkit`.

## Quick start (from source)

```bash
cd wa_toolkit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python wa.py login          # a Chromium window opens -> scan the QR with your phone
```

The login is stored in `./wa_profile/` (git-ignored). You never scan again unless you log out.
Then `python app.py` (desktop app) or the CLI commands below.

### Google Sheets (only for `collect`)

1. console.cloud.google.com → create project → *APIs & Services* → enable
   **Google Sheets API** and **Google Drive API**.
2. *IAM & Admin → Service Accounts* → create one → *Keys → Add key → JSON* →
   save as `service_account.json` in this folder.
3. Open your Google Sheet → **Share** → paste the service-account e-mail
   (`…@…iam.gserviceaccount.com`) as *Editor*.
4. Put the sheet ID (long string in the sheet URL) into `config.json → sheets.sheet_id`.

Don't want Google setup? Set `sheets.mode: csv` and you get `links.csv`.

## Commands

| Command | What it does |
|---|---|
| `python wa.py login` | first-time QR login, saves profile |
| `python wa.py send` | **Feature 1** – for each job in `config.json → send.jobs`, sends every line of the .txt as `line⏎text` into `send.chat` |
| `python wa.py send --chat "Name" --file file1.txt --text "https://…"` | same, ad-hoc |
| `python wa.py send --chat "Name" --message "hi"` | single message |
| `python wa.py collect` | **Feature 2+3** – for each group in `collect.groups`, scroll back `days`, keep links from `senders`, classify (platform + post/comment), append new rows to the sheet |
| `python wa.py collect --group "G" --sender "Rahul" --sender "Priya" --days 3` | ad-hoc filters |
| `python wa.py collect --metrics` | also try to fetch likes/comments/shares/views per link (slow, best-effort) |
| `python wa.py read --chat "G" -n 100` | dump the last 100 messages (great for checking sender names) |
| `python wa.py metrics <url>` | test the metrics scraper on one link |
| `python wa.py debug --chat "G"` | selector health-check + `debug.png` screenshot |

Sheet columns: `collected_at, group, sender, phone, sent_at, platform, kind, url,
clean_url, message, likes, comments, shares, views, metrics_note`.
Duplicates (same `clean_url`) are skipped on later runs, so you can run
`collect` daily and it only appends new links.

## How senders are recognised in groups

WhatsApp Web labels every group bubble with whatever *your phone* knows about
the sender: the **contact name** if the number is saved in your contacts,
otherwise the **phone number** (`+91 98765 43210`), sometimes prefixed with the
person's push-name (`~ Rahul`). The toolkit reads that label (`sender`) and,
when WhatsApp exposes it in the message id, the participant's number (`phone`).

`collect.senders` accepts either form and matches loosely:

* a value with 7+ digits is a number → compared on digits only, suffix match
  (`"9876543210"` matches `+91 98765 43210`)
* anything else is a name → case-insensitive, `~` ignored, "contains" match
  (`"rahul"` matches `Rahul Sharma`)

Numbers are the stable choice (names change if you rename a contact). To see
exactly what WhatsApp shows for each member, run
`python wa.py read --chat "Group" -n 100` — it prints a "Senders seen" list at
the end that you can copy into `config.json`.

## Link classification (`wa/links.py`)

| platform | post | comment |
|---|---|---|
| instagram | `/p/…`, `/reel/…` | `/p/…/c/<id>/` |
| twitter/x | `/status/<id>` | (replies are also `/status/` – not distinguishable from URL alone) |
| facebook | `/posts/`, `/share/p/`, `/videos/`, `permalink.php`… | `?comment_id=` / `?reply_comment_id=` |
| linkedin | `/posts/…`, `/feed/update/urn:li:activity:…` | `commentUrn=` |
| youtube | `watch?v=`, `youtu.be`, `/shorts/` | `&lc=` |
| threads / tiktok / reddit | supported | tiktok `?comment_id=`, reddit deep comment URL |

Add a platform by appending a rule to `PLATFORMS`.

## About metrics (likes / reach)

* WhatsApp does not know likes. `--metrics` opens each link on its own platform
  in a second tab of the same browser and scrapes what is publicly visible.
  Twitter/X uses a public endpoint; Instagram/Facebook/LinkedIn/Threads need you
  to be logged in in that browser (log in once in a normal tab of the automation
  Chromium). Layouts change often — expect `metrics_note` to say
  "not exposed" sometimes; everything else still works.
* **Reach / impressions are only visible to the post owner** on every platform,
  so they cannot be collected for other people's posts. Likes, comments, shares
  and (video) views are what's realistic.

## When WhatsApp Web changes its DOM

All CSS selectors live in `wa/session.py → SEL` as lists of alternatives. Run
`python wa.py debug --chat "Some group"`; anything printed as `MISS` needs a new
selector (right-click → Inspect in the automation window, add it to the list).

## Be nice to your account

Unofficial automation can get a number temporarily banned. Keep `delay ≥ 1.5 s`,
don't blast hundreds of messages, use it only on your own chats/groups.

## Task recipes — the flexible way (`tasks.json`)

`send` and `collect` are just shortcuts. Anything else you want is a **recipe**
in `tasks.json`: a named list of steps, run with

```bash
python wa.py tasks                                  # list recipes
python wa.py run collect_links                      # run one
python wa.py run collect_links --var days=2 --var 'senders=["Rahul","9876543210"]'
```

A recipe:

```json
"collect_links": {
  "vars": { "groups": ["G1", "G2"], "senders": [], "days": 7 },
  "steps": [
    { "action": "for_each", "list": "{groups}", "as": "group", "collect": "links", "steps": [
      { "action": "open_chat",     "chat": "{group}" },
      { "action": "read_history",  "days": "{days}", "max": 400, "save_as": "msgs" },
      { "action": "filter",        "from": "{msgs}", "senders": "{senders}", "has_links": true, "save_as": "msgs" },
      { "action": "extract_links", "from": "{msgs}", "kinds": ["post","comment"], "save_as": "links" }
    ], "save_as": "all_links" },
    { "action": "to_sheet", "from": "{all_links}" }
  ]
}
```

`{var}` / `{var.field}` placeholders read variables; `save_as` stores a step's
result; `for_each … collect` gathers a variable from every iteration.

| action | params | result |
|---|---|---|
| `open_chat` | `chat` | opens the chat, sets `{chat}` |
| `send_text` | `text`, `delay` | multi-line ok (`\n`) |
| `send_file` | `path`, `caption` | image / video / document |
| `send_lines` | `file`, `text`, `delay` | old script: each line → `line⏎text` |
| `read_history` | `days` / `since` (ISO), `max` | list of messages `{sender, phone, time, text, links, outgoing}` |
| `filter` | `from`, `senders`, `exclude_me`, `contains`, `regex`, `has_links` | filtered messages |
| `extract_links` | `from`, `kinds`, `platforms`, `dedupe` | list of link rows (platform, kind, clean_url, sender…) |
| `metrics` | `from`, `delay` | same rows + likes/comments/shares/views |
| `to_sheet` / `to_csv` | `from`, `dedupe` / `path` | rows written |
| `for_each` | `list` or `from`, `as`, `steps`, `collect` | loops; `{index}` available |
| `set` / `print` / `wait` | `name`,`value` / `text` / `seconds` | |
| `python` | `func` = `module.function` in `plugins/`, `args` | whatever your function returns |

When a step can't be expressed with the built-ins, write it in Python: drop a
file into `plugins/` (see `plugins/example.py`); functions receive
`(session, ctx, **args)`, where `session.page` is the live Playwright page, so
you can do anything WhatsApp Web can do. New built-in actions go in
`wa/engine.py → ACTIONS`.

## Desktop app (`app.py`) — for non-programmers

```bash
pip install pywebview && python app.py
```

Simple mode (default): the app opens the WhatsApp window for you. **Whatever chat
or group is open in that window is the target** — no names to type.

* **Send messages** — paste lines (or load a .txt), optional text under every
  message, delay → *Send to “<open chat>”*.
* **Collect links** — *Save this group to my list* (from the open chat), *Scan
  open chat for people* → tick senders, choose Post/Comment links, days back →
  *Collect links*. Results table → *Copy for Google Sheets* (paste with Cmd+V),
  *Save to CSV*, or *Save to Google Sheet* if configured.
* **Advanced** — recipes (`tasks.json`), JSON editor, debug tools, config.


A native window (pywebview — Electron-like, but Python, so it reuses everything
above; no Chrome extension needed) with the same features as the CLI:

```bash
pip install pywebview        # already in requirements.txt
python app.py
```

* **Run** tab — pick a task, tweak its vars, ▶ Run / ■ Stop, live log.
* **Edit task** tab — JSON editor for `tasks.json` (+ actions cheat-sheet), New / Save / Delete.
* **Tools** — Login (opens the WhatsApp browser), Read a chat (table of messages
  + clickable sender chips to copy names/numbers), Debug selectors, close browser.
* **Config** — edit `config.json` in place.

How it runs: the automation lives in one background worker thread that owns the
Playwright browser; the WhatsApp Web window opens separately (you can minimise
it) and stays open between tasks ("keep browser open" toggle in Tools). Tasks are
queued, so clicking Run twice runs them one after another. Closing the app closes
the browser.

### Packaging as a real .app / .exe (optional)

```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --name "WA Toolkit" \
  --add-data "ui:ui" --add-data "wa:wa" --add-data "plugins:plugins" \
  --add-data "config.json:." --add-data "tasks.json:." app.py
```

(on Windows use `;` instead of `:` in `--add-data`). Playwright's Chromium is
downloaded once per machine with `python -m playwright install chromium`; the
`wa_profile/` login folder is created next to the executable on first run.

## Command bot (`bot.py`) — drive it from your phone

```bash
python bot.py --group "Bot Control"      # or start it from the app's "Bot" tab
```

Runs **headless** (no window) using the saved login, and listens in that group.
From your phone, in the group: `/start` → reply `1` (messages only) or `2`
(messages + link) → send each *message list* as one WhatsApp message with a
**blank line between messages** (mode 2: send the list's link as the next
message) → `/run`. The bot sends list 1 message by message, then `1`, list 2 …
then `2`, etc. `/status`, `/cancel`, `/target Other Chat`, `/help`. Bot replies
start with 🤖. Only one browser can use the login at a time, so the app closes
its WhatsApp window when the bot starts (and vice-versa: stop the bot before
using Send/Collect in the app). If headless can't see WhatsApp on your machine,
start once with `--headed`.

## UI source (`ui-src/`)

The app UI is React (bundled with esbuild into `ui/bundle.js` — already built,
nothing to install for using the app). To change it:

```bash
cd ui-src && npm install && ./build.sh
```


## Building the installers (no Python needed by end users)

* **Windows** (on a Windows PC with Python 3.11+): `packaging\build_windows.bat` → `dist\WAToolkit\WAToolkit.exe`; with [Inno Setup](https://jrsoftware.org/isinfo.php) installed it also produces `dist\WAToolkit-Setup.exe`.
* **macOS**: `packaging/build_mac.sh` → `dist/WA Toolkit.app` + `dist/WAToolkit.dmg`.
* **No Windows machine?** Push the folder to a GitHub repo → *Actions* → *Build installers* → *Run workflow* → download `WAToolkit-Windows` / `WAToolkit-macOS` artifacts. (copy `packaging/github-build.yml` to `.github/workflows/build.yml` in the repo)

Chromium (~150 MB) is bundled inside; Windows only needs Edge WebView2 (already on Win10/11). First launch copies the default `config.json`, `tasks.json`, `plugins/` into the user data folder; the WhatsApp login (`wa_profile/`) is created there on first *Open WhatsApp*.

## Local version history

```bash
python checkpoint.py save "what changed"     # zip snapshot in checkpoints/ + entry in CHECKPOINTS.md
python checkpoint.py list | diff | restore NNN
```
Rule: every change ends with a checkpoint (RULEBOOK §5).

## Push to GitHub & get the Windows installer (step by step)

1. Copy the `wa_toolkit` folder to the laptop/account you want to push from (you can leave out `wa_profile/` — it is your WhatsApp login and is git-ignored anyway).
2. On github.com create an **empty** repository (no README).
3. In the folder run `packaging/push_to_github.sh <repo-url>` (mac/Linux) or `packaging\push_to_github.bat <repo-url>` (Windows). It copies the workflow into `.github/workflows/`, commits everything that is not in `.gitignore`, and pushes to `main`.
4. On GitHub: **Actions → Build installers → Run workflow**. ~10 min later download the `WAToolkit-Windows` artifact (contains `WAToolkit-Setup.exe`) and/or `WAToolkit-macOS` (`WAToolkit.dmg`).
5. On the Windows PC: run `WAToolkit-Setup.exe` → open *WA Toolkit* → **Bot tab**: tick *show browser*, Start bot once → scan the QR with the **bot's phone number** → after "Online" appears in the group, Stop, untick *show browser*, Start again (headless from now on). Data/login live in `%APPDATA%\WAToolkit`.

What gets pushed: source, `ui/` bundle, docs, `packaging/`, `config.json`/`tasks.json` (edit out anything private first), `CHECKPOINTS.md`. Never pushed: `wa_profile/`, `service_account.json`, `*.csv`, `checkpoints/*.zip`, build folders.

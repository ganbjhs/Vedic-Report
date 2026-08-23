# WA Toolkit — Rulebook

Roles, responsibilities and the strict rules every part of this project (and every
person or AI editing it) must follow. Read this before changing anything.
`BLUEPRINT.md` describes *how* it is built; this file describes *what must stay true*.

---

## 1. Roles & responsibilities

| Role | File(s) | Owns | Must never |
|---|---|---|---|
| **Session** | `wa/session.py` | launching Chromium with the persistent login profile; every CSS selector (`SEL`); reading the open chat's name (`current_chat`); the "logged-in" check; headless user-agent | contain business logic; be launched twice on the same profile |
| **Chat actions** | `wa/chat.py` | opening a chat by name (`open_chat`), typing/sending text (`send_text`), attaching files, the old line-by-line sender | read history; decide *what* to send |
| **Reader** | `wa/reader.py` | turning the DOM into message dicts (`read_visible`, `read_history`), expanding "Read more", incoming/outgoing detection, timestamps | send anything; click anything except "Read more" |
| **Links** | `wa/links.py` | URL cleaning + platform/kind classification (pure functions) | touch the browser |
| **Sheets** | `wa/sheets.py` | Google Sheets / CSV output with de-duplication on `clean_url` | contain WhatsApp logic |
| **Metrics** | `wa/metrics.py` | best-effort likes/comments/shares/views from the source platform | be required for anything else to work; claim "reach" for other people's posts |
| **Engine** | `wa/engine.py` | executing recipes from `tasks.json` (`ACTIONS`), `{var}` templating, plugins | hard-code chat names or credentials |
| **Paths** | `wa/paths.py` | where read-only resources vs writable data live (source vs frozen app) | be bypassed with `Path(__file__)` tricks elsewhere |
| **CLI** | `wa.py` | power-user commands (`login/send/collect/read/metrics/debug/tasks/run`) | be required by end users |
| **Desktop app** | `app.py` + `ui/` (React source in `ui-src/`) | the non-programmer experience: Send / Collect / Bot / Advanced; one worker thread owning Playwright; job queue; log | run Playwright from more than one thread; block the UI thread |
| **Command bot** | `bot.py` | the phone-driven workflow (`/start`, 1/2, lists, `/run` …), running headless as its own process | react to its own messages; run while the app's browser is open on the same profile |
| **Config** | `config.json` | every user-tunable value (profile dir, headless, delays, groups, senders, sheets, bot) | contain secrets other than the sheet id (credentials live in `service_account.json`) |
| **Recipes** | `tasks.json`, `plugins/` | user-defined automations | require code changes to add a new job |
| **Docs** | `README.md`, `RULEBOOK.md`, `BLUEPRINT.md`, `CHECKPOINTS.md` | truth about the project | fall behind the code (see rule 6) |
| **Checkpoints** | `checkpoint.py`, `checkpoints/` | local history / rollback | include `wa_profile/` or secrets |

---

## 2. Strict rules — WhatsApp & the browser

1. **One browser per login profile.** `wa_profile/` may be used by exactly one Chromium at a time. The app closes its window before starting the bot; the app refuses to open the browser while the bot runs. Never weaken this.
2. **Every selector lives in `wa/session.py → SEL` or in the JS inside `wa/reader.py`.** No selector strings anywhere else. When WhatsApp changes its DOM, that is the only place to fix, and `Debug selectors` (Advanced → Tools) is the way to find out what changed.
3. **Known DOM facts (Aug 2026)** — keep this list current when you learn something new:
   - messages: `#main [role="row"]` → bubble `[data-pre-plain-text="[6:33 AM, 8/18/2026] Sender: "]`, id from `[data-testid="conv-msg-<ID>"]`, text inside `.selectable-text`
   - `.message-in / .message-out` classes are gone → outgoing = a delivery tick aria-label (Delivered/Read/Sent/Pending) or a right-aligned bubble
   - emoji are `<img alt="…">` and single line-breaks are text `\n` → **never use `innerText`** for message text; walk nodes (see `textOf` in reader.py)
   - long messages need **"Read more" clicked repeatedly** (≈3000 chars per click) until the button disappears
   - header: line 1 = chat name, `span[title]` = members list (not the name)
   - search box: `input[data-tab="3"]`; composer: `#main footer div[contenteditable][data-tab="10"]` (Lexical); chat list: `#pane-side [data-testid="cell-frame-title"] span[title]`
4. **Headless is allowed only with a real desktop user-agent** (set in `WASession`) and only after a headed login has been saved. If headless can't see the chat list, fall back to headed; never loop forever.
5. **Never type into the search box when the target chat is already open** (`open_chat` checks `current_chat()` first) — search-typing every poll cycle is a bug, not a retry strategy.
6. **Be gentle with WhatsApp**: minimum 1 s between sent messages (`delay`, default 1.5), no bulk blasts to strangers, only your own chats/groups. Automation can get a number banned; the tool must never encourage abuse.
7. **The bot processes only the newest message** in the control group, only from *other* people (`ignore_own`), and marks its own replies with `BOT` (`🔹 `). It must never answer itself; a self-reply loop is a release-blocking bug.
8. **Read before you act**: any job that acts on "the open chat" must call `current_chat()` and refuse (`_need_chat`) when nothing is open.

## 3. Strict rules — data & privacy

9. `wa_profile/` (WhatsApp login) and `service_account.json` (Google key) are secrets: git-ignored, never checkpointed, never copied into builds.
10. Collected data (`links.csv`, Google Sheet rows) contains other people's names/numbers — keep it local, don't ship it, dedupe on `clean_url` so re-runs never duplicate.
11. Reach/impressions of other people's posts are **not obtainable** — the UI and docs must not promise them.

## 4. Strict rules — code & UX

12. **Non-programmer first.** Simple mode (Send / Collect / Bot) must never require typing a chat name, editing JSON, or reading a log. Anything technical goes under *Advanced*.
13. **The app never blocks**: all Playwright work runs on the single worker thread; the UI polls (`status`, `log_since`, `current_chat`, `bot_status`); long jobs stream progress to the log.
14. **Errors are readable**: every failure surfaces as one plain sentence in the UI/log (what failed + what to do), with the traceback only in "details".
15. **Paths go through `wa/paths.py`** so the same code runs from source and from the frozen `.exe/.app` (`APP_DIR` read-only, `DATA_DIR` writable).
16. **No new dependencies without need.** Current runtime deps: `playwright`, `pywebview`, `gspread`, `requests`. PyYAML is optional. Anything else must be justified in `BLUEPRINT.md`.
17. **Pure logic stays pure & tested**: `wa/links.py` (`test_links.py`), the bot state machine, the engine — they must be runnable without a browser (fake session) so they can be unit-tested.
18. **UI is React in `ui-src/`, built to `ui/bundle.js`.** Never hand-edit `ui/bundle.js`; edit `ui-src/app.jsx` and run `ui-src/build.sh`. Commit both source and bundle so users need no Node.

## 5. Strict rules — process

19. **Every change ends with a checkpoint**: `python checkpoint.py save "<what changed>"`. No exceptions, including doc-only edits.
20. **Every behaviour change updates the docs in the same checkpoint**: README (how to use), BLUEPRINT (how it works / DOM facts / contracts), RULEBOOK (if a rule changes).
21. **Selector fix = verify live first** (Debug selectors output or a browser probe), then patch `SEL`/reader JS, then checkpoint. No guessing selectors blind.
22. **Releases** are built by `packaging/` scripts or the GitHub Action, never by hand-copying files. Version bump goes in `packaging/installer.iss` (`AppVersion`) and CHECKPOINTS.

## 6. Definition of done for any task

- works from source (`python app.py` / `python bot.py`) **and** does not break the frozen-app path (`wa/paths.py`)
- unit tests still pass (`python test_links.py`, bot/engine offline tests)
- no new selector strings outside `wa/session.py` / `wa/reader.py`
- README / BLUEPRINT updated
- `checkpoint.py save` done

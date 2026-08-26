# Report bot — Telegram front door for Report Maker

Send it post links, it sends back the PDF. One job, end to end, with no
changes to `webapp/`: the bot signs in as an ordinary Report Maker account and
uses the same `/api` endpoints the browser uses.

```
Telegram ──► tg/bot.py ──HTTP──► Report Maker /api ──► capture ──► PDF ──► Telegram
```

## What it does

* **Links are read out of ordinary text** — several to a line, inside a
  sentence, in brackets, with a trailing full stop, or pasted without
  `https://`. A title and its links can share one message; the words become
  the report name and the links become the batch.
* **Send links however they arrive** — one message with twenty, or twenty
  forwarded messages with one each. The bot collects them and keeps a live
  count showing the project and style it will use, so **Run now** is never a
  leap of faith. Duplicates across messages are counted once.
* **Run now starts the job** — one tap, no confirmation step.
* **Nothing is lost by waiting.** If you don't tap anything, the batch closes
  itself after 30 seconds of quiet and becomes a confirm card — and links that
  arrive *after* that are still added to the same batch, not to a new one.
* **Type the report name as its own message first** if you like — it's
  remembered and used when the links follow.
* Or upload `.xlsx` / `.csv` / `.txt` instead; a file closes any open batch.
* **Everything is a tap.** Project and style are inline-keyboard pickers, and
  your last choice is remembered per person (`tg/data/prefs.json`), so the
  second report is one tap. A picker with a single answer is skipped entirely.
* It runs `/api/preview` first, so you see **what would be captured** —
  link count, duplicates removed, rows skipped — before anything is spent.
* Tap **Run**. One message edits itself with a progress bar until the job ends.
* The PDF arrives as a document. Word / PowerPoint / screenshots appear as
  buttons and are fetched on demand — no re-capture.
* First line of your message becomes the report name. No line, no question:
  it names it `Report DD-MM-YY HH:MM`.

## Using it in a group

Add the bot to the group, then **tag it** to start a batch:

```
@vedicreport_bot July fake accounts
https://x.com/…/status/…          ← no tag needed from here on
https://x.com/…/status/…
```

The tag opens the batch; every link after that is collected without tagging
until the report runs. Replying to one of the bot's own messages counts as a
tag too. Untagged chatter is ignored entirely.

In a group the project, the style and the batch belong to the **chat**, not to
each person — one team, one report, and the PDF lands in the group. In a
private chat that is the same thing, so nothing changes there.

**One BotFather setting is required:** `/setprivacy` → pick the bot →
**Disable**. Otherwise Telegram only delivers messages that mention the bot,
and the untagged follow-up links never reach it. The bot still ignores
everything it is not addressed in — the filtering is its own, not Telegram's.

## Set up (local, 5 minutes)

**1 · Make the bot.** In Telegram, talk to **@BotFather** → `/newbot` → name it
→ copy the token.

**2 · Give it a Report Maker account.** In the project's main `.env`, add one
to `APP_USERS`:

```
APP_USERS=tilak:yourpassword,reportbot:pick-a-long-password
```

**3 · Configure.**

```bash
cd tg
cp .env.example .env
# fill in TG_BOT_TOKEN and APP_PASSWORD
```

**4 · Install and run.**

```bash
../.venv/bin/pip install -r requirements.txt
../.venv/bin/python bot.py
```

Report Maker itself must be running:

```bash
.venv/bin/python -m uvicorn webapp.main:app --port 8000
```

**5 · Lock it down.** Message the bot `/whoami`, put the id it gives you into
`TG_ALLOWED_IDS`, restart. An empty allowlist means anyone can use it — fine on
your laptop, not on the VPS.

## Commands

| | |
|---|---|
| *(just send links)* | preview → Run → PDF |
| *(upload a file)* | same, from `.xlsx` / `.csv` / `.txt` |
| `/project` | pick the project runs go into |
| `/style` | pick the style reports print in |
| `/whoami` | your Telegram id, for the allowlist |
| *(a short line with no link)* | remembered as the report name |
| `/start` | the short how-to, and what you're currently set to |

## Things worth knowing

* **The project defaults to whatever is selected in the dashboard** until you
  pick one with `/project`; after that your own choice sticks.
* **Style is only asked when the project has more than one.** A style that was
  deleted from the pool after being picked is never offered.
* **The platform comes from the style, not from the links.** A Twitter style
  captures X links, a Combined style takes X/Facebook/Instagram mixed — which
  is the pairing the server itself checks. Changing the style re-runs the
  preview, so the counts on the card always match what will actually run.
* **50 MB** is Telegram's document ceiling. Bigger reports come back as a link
  to the job page instead.
* **Sessions expire.** The client re-signs-in by itself on a 401.

## What's next

This is v0 on purpose — one bot, one job, no scoped tokens. The upgrade path:

1. `/v1` API + bot registry in the dashboard (scopes per bot), then
   `client.py` swaps to a token and `APP_USER`/`APP_PASSWORD` disappear.
2. Source → date wizard, so a run needs no links at all — it reads the
   project's Google Sheet.
3. Second bot: message splitter. Third: the group link collector.

# Report bot — Telegram front door for Report Maker

Send it post links, it sends back the PDF. No changes to `webapp/`: the bot
signs in as an ordinary Report Maker account and uses the same `/api`
endpoints the browser uses — so every run also shows up in History, attributed
to `reportbot`.

```
Telegram ──► tg/bot.py ──HTTP──► Report Maker /api ──► capture ──► PDF ──► Telegram
```

## The conversation

```
/start    →  New report                        [Change project] [Change style]
             Project   Kashi Report            [Continue]
             Style     Combined Report

Continue  →  Send the post links.

links     →  12 links received                 [Yes, continue] [Not yet]
             Finished adding?

Not yet   →  12 links                          (asks again when more arrive)
             Still listening — send the rest.

Yes       →  Name this report                  [Use today's date]

name      →  July fake accounts                [Run report] [Edit]

             Project   Kashi Report
             Style     Combined Report
             Links     12   2 duplicates removed
             Time      about 2 minutes

             Requested by @tilak

Edit      →  What needs changing?
             [Project] [Style] [Name] [Add links] [Back]

Run       →  ████████░░░░░░░░   50%             [Stop]
             Capturing posts · 6 of 12

          →  Complete   12 posts · 1m 48s      [Word] [PowerPoint] [Screenshots]
             + the PDF
```

One short question at a time, buttons for the answers, the whole exchange in a
single message that keeps being rewritten. Formats other than PDF build on
demand from the screenshots already taken — no re-capture.

**Attribution.** Whoever sends the first link is recorded as the requester and
printed on the summary, the progress and the finished report. If someone else
presses Run, both names appear — useful in a shared group.

**Commands.** `/start` builds a report · `/last` brings back the most recent
one from this chat with its download buttons · `/cancel` discards the one in
progress.

**Links** are read out of ordinary text: several to a line, inside a sentence,
in brackets, with a trailing full stop, or pasted without `https://`.
Duplicates across messages count once. A `.xlsx` / `.csv` / `.txt` upload works
too and skips straight to the summary.

**Project and style** default to whatever the dashboard has selected, then to
the chat's last choice. A picker with one answer is skipped. The **platform
comes from the style** — a Twitter style captures X links, a Combined style
takes mixed — which is the pairing the server itself enforces.

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

# Telegram — the bots that replace the WhatsApp toolkit

*Written 26 Aug 2026. Status: **bot 1 (report) is live**; bots 2 and 3 are
designed, not built. Companion docs: `tg/README.md` (how the report bot works
today), `wa/BLUEPRINT.md` (what is being replaced), `docs/v3-plan.md` (the API
layer this leans on).*

---

## 0. Why Telegram, in one paragraph

WhatsApp has no API, so `wa/` drives a real Chromium with a saved login: a
second phone number, a profile folder, CSS selectors that break when the DOM
changes, a one-browser-at-a-time lock that competes with the capture engine for
RAM, and a standing ban risk. Telegram is a plain HTTP API. Moving to it does
not add a second stack — it **deletes** one. The server goes from two browser
stacks to one, and the freed memory goes to the thing that earns money.

What the WhatsApp tool did that still matters:

| `wa/` did | On Telegram |
|---|---|
| Scrape links out of group messages | The bot is *in* the group and receives every message as structured data — sender, timestamp, URLs pre-extracted |
| Send a list of messages into a chat | One API call each, no ban-safety delay, editable and deletable afterwards |
| A second WhatsApp number sitting in a control group running a `/start … /run` state machine | A button flow, no second SIM, no state machine to maintain |

What is lost: **nothing that was being used.** The one real difference is that a
Telegram bot cannot cold-message a person who has never started it. Every
current use is "post into our own groups", so that costs nothing.

---

## 1. Shape

Three bots, three `@usernames`, three DM windows — **one backend**.

```
        ┌── @vedicreport_bot  (report) ──┐
Users ──┼── @<splitter>_bot   (send)   ──┼──►  one API + one queue + one Chromium pool
        └── @<collector>_bot  (listen) ──┘              │
                 ▲                                      ▼
            hub group ◄──────────── results announced back
```

Separate tokens because each deserves its own DM window and its own menu; one
codebase because they share the API client, the panel/state machine, the
permission model and the deployment. A fourth bot is a token and a checkbox,
not a build.

**The hub group** is a pinned message with one deep-link button per bot. The
bots do not need to be members for that to work. Each bot has an *announce
results to the hub* setting for work done in a DM that the team should see.

---

## 2. Permissions

Two layers that intersect. This is the whole safety story in one line:

```
effective permission  =  the bot token's scopes  ∩  the acting person's role
```

A bot can never exceed its token; a person can never exceed their own account.
One bot can therefore serve everybody safely, and "who may send messages on
behalf of the team" is answered in exactly one place.

### 2.1 The bot registry (dashboard → Admin → Bots)

```
Bot
  name, kind (telegram | generic | webhook), token_hash, status
  projects[]   which projects it may touch, or "all"
  scopes[]     what it may do
  limits       links/call, runs/hour, MB/day, max concurrent
  audience     allowed Telegram user ids and group ids
```

New SQLite tables: `bots`, `bot_scopes`, `link_codes`, `tg_identities`,
`api_calls`.

### 2.2 Scopes

| Report | Send | Collect | Admin |
|---|---|---|---|
| `report.preview` | `send.compose` | `collect.read` | `style.write` |
| `report.run` | `send.dispatch` ⚠️ | `collect.write` | `schedule.write` |
| `report.download` | `send.other_chat` ⚠️ | | `users.link` |
| `report.cancel` | | | |
| `project.read` · `source.run` | | | |

Presets so it is one click: **Viewer / Operator / Publisher / Admin**.
`send.dispatch` is its own scope because it is the only one that can push
messages into other people's chats.

### 2.3 The API this rides on

`docs/v3-plan.md §11` promised one key per project. The bot registry is that,
generalised. Endpoints under `/v1`, `Authorization: Bearer vr_…`, plus an
`X-Actor: tg:<id>` header so the server knows *which colleague* is acting
behind the bot — that header is what makes the intersection and the audit log
work. Every call rows into `api_calls` (bot, actor, scope, run, result).

**Until `/v1` exists**, the report bot signs in as an ordinary `APP_USERS`
account and uses the same `/api` endpoints the browser does. That is a
deliberate stopgap: it required no server change, and it means every run shows
in History attributed to `reportbot`. When `/v1` lands, only `tg/client.py`
changes.

---

## 3. Bot 1 — Report  *(live)*

Full behaviour in `tg/README.md`. In short: `/start` → project and style →
send links (pasted, forwarded one at a time, or a spreadsheet) → *Done?* →
name → a summary card showing project, style, name, link count, estimate and
who asked → **Run** → progress bar → every format the style builds offered as
buttons, each fetched on demand.

Decisions worth not relitigating:

* **State is per chat, not per person.** In a group the team shares one report.
* **The platform comes from the style**, never guessed from the links — that is
  the pairing the server itself enforces.
* **Never ask a question with one answer.** One project, or one style, and the
  picker is skipped.
* **Attribution** follows the run: requester, and the runner too when they
  differ.
* **No emoji.** It is a tool colleagues use weekly, not a novelty.
* **Delivery stays where it was asked for.** One PDF is not noise, and in a
  group it is usually the point that everyone sees it.

---

## 4. Bot 2 — Message splitter  *(next)*

### 4.1 What it replaces

`wa/bot.py`'s job: take a batch of **message lists**, send every message of
list 1 into a chat one at a time, then send `1`, then all of list 2, then `2`,
and so on. The numbering is the point — it marks where one list ends in the
destination chat.

The WhatsApp input shape was `/start` → mode `1` (messages only) or `2`
(messages + a link under each) → one WhatsApp message per list with blank lines
splitting it into messages → `/run`. Plus `/status`, `/cancel`,
`/target <chat>`.

There is also a simpler sender in the same tool not to be confused with it —
the `forward_links` recipe (`send_lines`), where every *line* of a `.txt`
becomes its own message with one fixed piece of text appended under each. Two
different splitting rules; the bot should offer both rather than pick one.

### 4.2 The conversation moves to DM

The output is many messages, and the *composing* is chatty too — the counter,
the mode question, the confirm card. In a group that is a thread of bot
messages before a single message has been sent.

So: **tag it in the group and it moves the conversation to your DM.** The group
sees one line — *"Continue in your DM ↗"* with a button — and everything after
that happens privately. That button is also the deep link that fixes the
never-started-the-bot case, sitting exactly where people will naturally tap it.
If they decline, it falls back to a single self-editing message in the group.

### 4.3 Destination

```
Send to:   [ My DM ]   [ This group ]   [ Another chat… ]
```

* **My DM** — the default. Quietest, and the reason this bot exists separately.
* **This group** — where the request came from.
* **Another chat…** opens a list built from two registries:
  * **chats** the bot is a member of (`id → title`, remembered whenever the bot
    is added to a chat or used in one — Telegram gives no directory, so a chat
    the bot has never seen cannot be named);
  * **people** who have used the bot, listed by `@username`.

Remembered per person, so the regular case is zero taps.

**The one hard limit:** a bot cannot open a conversation. Sending to a person
who has never pressed Start fails with *"bot can't initiate conversation with a
user"*. Handle it as guidance, not an error — reply with a *Start me* deep
link, and mark that person's entry in the destination list as "needs to start
the bot" rather than letting the send fail.

**Sending to another person's DM** is a small harassment vector and an easy
accident. Gate it behind the `send.other_chat` scope, stamp every delivery with
*"sent by @tilak"*, and keep people who have not opted in out of the list.

### 4.4 Receipt

When the delivery goes somewhere other than where you are standing, you need a
receipt in your DM: *"Sent 24 messages in 3 lists to Engagement Group 1."*
Nobody should have to switch chats to check it worked.

### 4.5 Pacing

No ban-safety delay is needed, but pacing still is, for a different reason:
Telegram allows roughly **20 messages per minute into one group** (about 30/s
overall across chats). A 60-message batch therefore takes ~3 minutes no matter
what. The bot paces itself, shows a progress bar, and never fires a burst that
gets it throttled mid-list — a throttle that split a list in half would be
worse than slowness. Being able to edit and delete after sending is new and
worth using for a "sent the wrong list" undo.

### 4.6 Open decisions

1. Split rule: blank-line (the WhatsApp convention colleagues know) or
   per-line, or offered as a choice per batch?
2. Numbering: plain `1`, `2`, `3` as today, or something like `— 1 —`?
3. Does the destination belong to the person or to the chat?

---

## 5. Bot 3 — Group link collector

Lives in the link groups and turns them into a project source.

* **Privacy mode off** in BotFather (or make it an admin), so it receives every
  message rather than only mentions.
* It is not automating anything — Telegram hands it each message as an object:
  URLs already extracted, stable sender id, exact timestamp, forward origin,
  reply thread, and in forum groups the topic.
* Classification is rules over fields: platform and post/comment (reuse
  `profiles/netlinks.py`), sender, group, forum topic, keyword. Manual mode:
  the bot replies under each captured link with buttons (*Fake accounts ·
  Positive · Skip*) and whoever is in the group taps one — that becomes the
  section in the report.
* Output lands deduped in the project's source, so the report just runs on it.

**Two honest limits:** the bot must be *in* the group, and it gets no history
from before it joined. Day it joins is day one of data.

**This is what closes the loop.** Collector fills the sheet → the report bot
already reads that sheet as a project source → the PDF lands in the hub group.
Nobody copies a link anywhere.

---

## 6. Build order

| Phase | Scope |
|---|---|
| **done** | Report bot on the `/api` stopgap: pickers, link collection, summary card, run, all formats on demand, attribution, group support, skipped-link reporting |
| **1** | `/v1` + bot registry: tables, Admin → Bots page, token auth, scopes, audit log; then `/v1/run`, status, download; `tg/client.py` swaps to a token |
| **2** | Message splitter (§4) |
| **3** | Source → date wizard, so a recurring report needs no links at all |
| **4** | Group collector (§5) |
| **5** | Scheduled delivery — sources already auto-run on a new date; point that at a chat and the PDF simply arrives |

Worth doing alongside: queue position when a run waits behind another
(`MAX_CONCURRENT_JOBS=1`, so a busy morning means staring at "Queued"), and a
`/help` that is honest about what each bot will and will not do.

---

## 7. Deployment

One extra service in `docker-compose.yml` per bot, all on `python:3.12-slim` —
no Playwright, no Chromium, tens of megabytes, starts in a second. Long
polling, so nothing is published and no TLS route is needed.

Two things that have already bitten us and will again:

* **`COOKIE_SECURE=1` marks the session cookie Secure.** Browsers make an
  exception for `localhost`; HTTP clients do not. A bot pointed at
  `http://web:8000` never gets the cookie back and every CSRF check fails as
  *"this form expired"*. Point bots at the HTTPS address through Caddy.
* **A bot must never die on a failed sign-in.** `restart: unless-stopped`
  brings it straight back, and seconds of that trips Report Maker's own login
  rate limiter — the bot locks itself out of the app it is trying to use. Log
  it, start anyway, sign in lazily.

Only one instance of a given token may poll at a time. Stop the laptop copy
before starting the server one.

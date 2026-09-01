# Telegram — the bots that replace the WhatsApp toolkit

*Written 26 Aug 2026. Status **1 Sep**: **bot 1 (report) is live**; **`/v1` +
the bot registry are built** (phase 1); **bot 2 (splitter) is built** (phase 2);
bot 3 is designed, not built. Companion docs: `tg/README.md` (the report bot),
`tg/SPLITTER.md` (the splitter), `wa/BLUEPRINT.md` (what is being replaced),
`docs/v3-plan.md` (the API layer this leans on).*

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

**Built, 1 Sep.** `/v1` is `webapp/api_v1.py`, the registry is
`webapp/routes_bots.py` + Admin → Bots, and the tables are in
`webapp/jobs/store.py`. Endpoints: `whoami`, `link`, `projects`, `preview`,
`run` (JSON links or a multipart spreadsheet), `run/{id}`, `cancel`,
`download/{kind}`.

The promise held: **only `tg/client.py` changed**, which gained a `TokenClient`
beside the old `ReportMaker`, plus one four-line handler in `bot.py` that
records who is acting. Setting `RM_TOKEN` in `tg/.env` switches the report bot
over; leaving it unset keeps the `APP_USERS` sign-in exactly as it was. A live
bot deserves a swap that is one line of `.env` and reversible.

Two things worth not relitigating:

* **An unlinked actor gets the empty set, not the token's scopes.** A stranger
  who finds the bot can do nothing at all. That is the whole reason the header
  exists, so failing open there would have been the one bug that mattered.
* **The audit log records refusals too.** A log of successes cannot answer the
  question a log exists for.

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

## 4. Bot 2 — Message splitter  *(built — `tg/splitter.py`, `tg/SPLITTER.md`)*

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

*Built as:* the scope gate and the exclusion hold. The stamp became **one line
before the batch** rather than a suffix on every message — appending it to
sixty messages would quietly rewrite the very content the bot exists to deliver
verbatim, and one line answers "who sent this" just as well.

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

### 4.6 Decisions taken (1 Sep)

1. **Split rule: both, chosen per batch.** `[Blank line] [Per line]`, remembered
   per person. §4.1 already noted the WhatsApp toolkit had two rules and both
   were in use; picking one for people would have broken half of what they do.
   Changing the rule mid-batch **re-splits what is already collected** — a batch
   split two different ways without saying so would be the worst outcome.
   Alongside it: *Under each* — nothing, a link per list, or one fixed text —
   which is what folds the `send_lines` recipe in rather than bolting it on.
2. **Numbering: plain `1`, `2`, `3`.** Unchanged from `wa/bot.py`. Colleagues
   already read that marker in the destination chat; making it prettier would
   change something people rely on to buy nothing.
3. **The destination belongs to the person.** The opposite of the report bot,
   and deliberately: a report is one thing a team shares, while a batch of
   messages is one person's outbound work, composed in their own DM. Since the
   compose flow moves to the DM anyway, per-chat would have collapsed to
   per-person with extra steps.

Four more that came out of building it, three of them from first use:

-1. **Features are a registry, not a fork.** Asked to switch Preview off but
   not delete it, the honest shape is a flag rather than a deletion: code cut
   to "simplify" is code somebody rewrites from memory later. A bot now
   **announces its own feature catalogue** to `/v1/features` at start-up and
   Admin → Bots renders switches for whatever arrived — the server hardcodes no
   feature names, so this works for the report bot and the collector without
   further server work. Announce is authenticated by the **token alone**, no
   actor and no scope: a bot boots before anybody has pressed a button, so
   requiring an actor would mean it could never start.

   The line that keeps this honest: **a feature says whether something is
   present; a scope says whether you are allowed to do it.** `send.other_chat`
   stays a scope. Anything that could hurt somebody is an administrator's
   decision and belongs in the audit log, not in a convenience toggle.


0. **A list may span several Telegram messages.** The original design — one
   Telegram message per list — does not survive contact with the 4096-character
   ceiling: a long list physically cannot arrive in one paste, and it does not
   survive clients that drop blank lines on paste either. So messages
   accumulate into the list being built, and **a message that is only a number
   closes it** — the same marker the bot prints on the way out, so there is
   nothing new to learn. Only a whole message counts, never a line inside a
   paste. *Done* closes whatever is open.

   This subsumes the old rule rather than competing with it: several messages
   and no numbers is one list, several messages with numbers is several lists,
   and one message is what it always was.


4. **Messages over Telegram's 4096-character limit are refused, not truncated.**
   A message cut in half on delivery is worse than one never sent, because
   nobody notices.
5. **Undo, for 30 minutes.** Telegram allows deletion for 48 hours, but an undo
   button sitting next to yesterday's work is a trap rather than a feature.

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
| **done** | `/v1` + bot registry: tables, Admin → Bots page, token auth, scopes, audit log, `/v1/run` + status + download; `tg/client.py` gained `TokenClient` and switches on `RM_TOKEN` |
| **done** | Message splitter (§4) — `tg/splitter.py`, its own token, its own compose panel, paced delivery, undo |
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

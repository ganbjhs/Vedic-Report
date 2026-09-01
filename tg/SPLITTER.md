# Message splitter — Bot 2

Takes a batch of message **lists** and sends every message of list 1 into a
chat one at a time, then `1`, then all of list 2, then `2`, and so on. The
numbering is the point: it marks where one list ends in the destination chat.

This is `wa/bot.py`'s job without the WhatsApp. No second phone number, no
Chromium, no ban-safety delay, no CSS selectors that break when a DOM changes —
one HTTP API, a container of a few tens of megabytes, and messages you can edit
and delete after sending.

```
Telegram ──► tg/splitter.py ──► one message at a time, paced ──► the chat
                   │
                   └── (optional) Report Maker /v1 — may this person send
                       into a chat they are not standing in?
```

## The conversation

Tag it in a group and it moves the conversation to your DM. The group sees one
line — *Continue in your DM ↗* — and everything after that happens privately.
That button is also the deep link that fixes the never-started-the-bot case,
sitting exactly where people will naturally tap it. In a DM, `/start`.

```
/start      →  New batch                       [Split] [Under each]
               Split       Blank line          [Send to]
               Under each  Nothing             [Continue]
               Send to     your DM

Continue    →  Send your lists.
               Send as many messages as you like — inside each one, a
               blank line starts a new message. They all join the list
               you are building. Send 1 on its own to close that list
               and start the next.

<paste>     →  1. 7 messages · still open      [Close this list]
<paste>     →  1. 12 messages · still open     [Done] [Start over]
1           →  1. 12 messages
<paste>     →  1. 12 messages
               2. 5 messages · still open

Done        →  Ready to send                   [Send]
               Messages    12                  [Preview] [Edit]
               Lists       2
               Split       Blank line
               Under each  Nothing
               Send to     Engagement Group 1
               Time        about 1 minute

Send        →  ████████░░░░░░░░  7 of 14       [Stop]
               List 1 of 2

            →  Sent 12 messages in 2 lists     [Undo — delete them]
               to Engagement Group 1.          [New batch]
```

## A list can span several messages

Telegram caps one message at **4096 characters**, so a long list simply cannot
arrive in one paste. Messages therefore **accumulate into the list you are
building**, and a message that is *only a number* closes it:

```
<paste part 1>
<paste part 2>
<paste part 3>
1                ← list 1 is now those three pastes together
<paste>
2                ← list 2
Done             ← closes anything still open, then prices the batch
```

It is the same marker the bot prints between lists on the way out, so there is
nothing to learn: you type what you will see. `1`, `1.`, `1)`, `- 1 -` and
`— 1 —` all read the same. Only a message that is *entirely* a number counts —
a line reading `1` inside a pasted block is content, which is what makes the
rule safe to state in one sentence.

**Every button has a typed equivalent**, because reaching for a button when
your hands are already in the message box is the slow way round:

| Type, as a whole message | Same as |
|---|---|
| `1` (any number) · `close` · `end` | **Close this list** |
| `done` · `finish` · `finished` | **Done** |

Case and trailing punctuation do not matter — `DONE`, `Done.` and `done` are
one thing. The whole-message rule applies here too: `done deal` is content.
Nothing **destructive** has a typed word — a stray `stop` that threw away a
300-message batch would be unforgivable, so cancelling stays on `/cancel`.

If you never send a number, everything you pasted is one list. **Done** closes
whatever is open, so no trailing number is needed. There is a *Close this list*
button too, for finding out that closing is a thing at all.

## The two splitting rules

The WhatsApp toolkit had two and both were used, so both are offered — as one
tap per batch, not a decision made for you:

| Split | What it does | For |
|---|---|---|
| **Blank line** | one blank line between messages | the convention colleagues already type |
| **Per line** | every line is its own message | a list of links (the old `send_lines` recipe) |

And what goes under each message:

| Under each | |
|---|---|
| **Nothing** | just the message |
| **A link per list** | you send the list, then its link; it goes under every message of that list |
| **One fixed text** | the same line under every message in the whole batch |

Changing the split rule re-splits what you have already collected. Leaving the
earlier lists cut the old way would send one batch split two different ways
without saying so.

## Destination

```
Send to:   [ My DM ]   [ This group ]   [ Another chat… ]
```

Remembered **per person**, so the regular case is zero taps.

*Another chat…* is built from two registries, because Telegram gives a bot no
directory: **chats** it has been added to or used in, and **people** who have
pressed Start. A chat it has never seen cannot be named, and it says so rather
than showing a list of numbers.

**A bot cannot open a conversation.** Sending to somebody who has never pressed
Start fails with *"bot can't initiate conversation with a user"*. That is
handled as guidance, not an error: the reply says to ask them to press Start
first.

**Sending into another person's chat** is a small harassment vector and an easy
accident, so it is gated on its own permission:

* With `RM_TOKEN` set, on the `send.other_chat` scope in Admin → Bots. Such a
  delivery is preceded by one line — *Sent by @tilak* — so the chat knows who
  it came from.
* Without one, on the `TG_SEND_OTHER_IDS` allowlist — the same deliberate
  stopgap the report bot shipped with.

## Pacing, and why it takes as long as it does

Telegram allows roughly **20 messages a minute into one group** (about 30 a
second overall). A 60-message batch therefore takes about three minutes no
matter what any setting says. The bot paces itself, shows a progress bar, and
never fires a burst that would get it throttled mid-list — a throttle that
split a numbered list in half would be worse than slowness. If Telegram asks it
to wait anyway, it waits and carries on rather than abandoning the list.

**Undo** deletes everything it just sent, for 30 minutes afterwards. This is new
— on WhatsApp a wrong list was simply in the chat for ever — and it is the
answer to "I sent the wrong batch".

## Features

Anything below that can be switched off **is switched off, not removed** — code
deleted to simplify is code somebody has to rewrite from memory when they want
it back. Each flag resolves in layers, most specific last:

```
the built-in default  →  TG_FEATURE_<NAME> in the env  →  the bot registry
```

`/features` prints what is on and **where each answer came from** — because the
first question when something is missing is always "is it off, or is it broken?"

| Feature | Default | |
|---|---|---|
| `preview` | **off** | A dry-run listing before sending. Off because Undo already answers "that was the wrong batch", and it answers it *after* the fact, which is when people actually notice. |
| `text_commands` | on | `done` and `close` typed as a whole message |
| `undo` | on | delete the batch after sending |
| `receipt` | on | a line back in your DM when the delivery went elsewhere |
| `fixed_text` | on | the one-fixed-text-under-everything recipe |

```bash
TG_FEATURE_PREVIEW=on        # bring it back
TG_FEATURE_TEXT_COMMANDS=off # make 'done' ordinary content again
```

### From the dashboard

With `RM_TOKEN` set, the bot **announces its own feature list** to `/v1` when
it starts, and **Admin → Bots** shows those features with on/off switches. The
dashboard hardcodes no feature names — a feature added to any bot appears there
by itself the next time that bot restarts, which is the whole point of doing it
this way.

* A switch has **three** states: on, off, and *use default* — handing the
  decision back to the bot is not the same as switching the feature off, and
  the page says which one is in force.
* **Re-announcing never undoes an admin.** A redeploy refreshes labels and
  defaults, and leaves overrides alone.
* A feature the bot **stops announcing** disappears from the page, so there is
  never a switch controlling nothing.
* The bot re-reads the switches every five minutes (`TG_FEATURE_REFRESH`).
  `/sync` pulls them immediately — which is what you want right after flipping
  one.
* If the dashboard is unreachable, the env and default layers still resolve and
  the bot carries on. A bot that stopped working because a config server was
  down would be a worse bot.

Only administrators can flip a switch: same gate as scopes and limits, so
there is one page and one answer to "who configured this".

Note what is *not* a feature: **`send.other_chat` is a scope, not a flag.** A
feature flag says whether something is present; a scope says whether you are
allowed to do it. Anything that could hurt somebody stays on the scope side,
where switching it off is an administrator's decision and is written to the
audit log.

## Commands

| | |
|---|---|
| `/start` | a new batch (in a group: moves to your DM) |
| `/features` | what is switched on, and where the setting came from |
| `/sync` | pull the switches from the dashboard now |
| `/status` | what is collected so far |
| `/cancel` | discard it, or stop a delivery in progress |
| `/whoami` | your Telegram id and this chat's id, for the allowlists |
| `/link <code>` | bind your Telegram id to a Report Maker account |

## Set up

**1 · Make the bot.** @BotFather → `/newbot` → copy the token. Leave privacy
mode **on**: unlike the report bot, this one only ever acts when tagged, so it
has no reason to read a group's chatter.

**2 · Configure.**

```bash
cd tg
cp .env.splitter.example .env.splitter
# fill in TG_SPLITTER_TOKEN, and TG_ALLOWED_IDS once you know your id
```

**3 · Run.**

```bash
../.venv/bin/python splitter.py
```

## Putting it on the server

**`.env` files never travel.** They are gitignored, so `deploy.sh` (a `git
fetch` + `git pull`) never carries one, and they are dockerignored, so nothing
is baked into the image either — `env_file:` is read from the server's own disk
each time the container starts. So the token has to be entered **once on the
laptop and once on the server**, by hand:

```bash
# on the server, in the repo
cp tg/.env.splitter.example tg/.env.splitter
nano tg/.env.splitter          # TG_SPLITTER_TOKEN, TG_ALLOWED_IDS
chmod 600 tg/.env.splitter
./deploy.sh
```

`deploy.sh` looks for that file and turns the `splitter` compose profile on by
itself when it is there. Until it exists the splitter is simply not started,
and the rest of the stack comes up exactly as before — the file is deliberately
**not** a hard `env_file` on an always-on service, because a missing one would
fail `docker compose up` for the web app too.

To run it by hand: `docker compose --profile splitter up -d tg-splitter`.

**Same token, two machines, is the one thing that breaks it.** Telegram allows
only one poller per token — a laptop copy left running will steal updates from
the server's. Stop the local one before deploying.

**4 · Lock it down.** Message it `/whoami`, put the id into `TG_ALLOWED_IDS`,
restart. An empty allowlist means anyone who finds the bot can use it.

Only one instance of a given token may poll at a time — stop the laptop copy
before starting the server one.

## Things worth knowing

* **State belongs to the person, not the chat.** The opposite of the report
  bot, and for a reason: a report is one thing a team shares, while a batch of
  messages is one person's outbound work, composed in their own DM.
* **A batch is capped** at 400 messages. Past that the honest answer is "send
  this one, then start another", not a delivery that runs for twenty minutes.
* **"Blank line" only works if the blank line survives the paste.** Phone
  keyboards, note apps and browsers routinely leave a non-breaking or
  zero-width space on the line that *looks* empty, and some clients strip the
  gap entirely. The splitter treats CRLF endings and invisible-space lines as
  blank, so most of those pastes now split as they look. If a paste still comes
  back as **1 message** when you expected several, the blank lines did not
  survive — switch to **Per line**, or close lists with numbers instead.
* **Messages over Telegram's 4096-character limit are refused, not truncated.**
  A message cut in half on delivery is worse than one that was never sent,
  because nobody notices.
* **The bot is never a member of anything it does not need to be.** It sends
  into a group by id, which it learned by being added there once.
* **Attribution is one line before the batch, not a suffix on every message.**
  Appending *sent by @tilak* to sixty messages would quietly rewrite the very
  content this bot exists to deliver verbatim.

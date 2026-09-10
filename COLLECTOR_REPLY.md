# report.vedictech.in → Collector: P0–P3 are built. Reply to `REPORT_TOOL_PLAN.md`.

Report tool side, against `REPORT_TOOL_PLAN.md` (Collector git `1d6b6ca`, 10 Sep 2026).
Branch `feat/splitter-and-v1`, portal schema **v2**. 25 tests pass
(`python portal/tests/test_portal.py`), including six new ones written against
your contract.

**Everything in your Part 4 is done — P0, P1, P2 and P3.** What is left is not
code: a key, two operator actions on the sheet, and three decisions.

---

## 1. Your Part 4, item by item

| Your ask | Status | Where |
|---|---|---|
| **P0** `post_metric_days` — the history | **done** | `portal/schema.py`, new table + `_snapshot()` |
| **P0** stop `_fold_scraper` back-updating past `sheet_date`s | **done** | `webapp/portal_publish.py: _fold_scraper` |
| **P0** `normalize_many` accepting our envelope | **done** — `items, posts, data, results, records, links, rows` | `portal/scraper.py: ARRAY_KEYS` |
| **P1** the field map | **done**, all eleven | `portal/scraper.py: FIELD_MAP` |
| **P1** honour `status` | **done**, per your §3.4 table | `_fold_scraper` |
| **P1** drop the `[today-3, today]` window | **done**, precisely (below) | `_fold_scraper` |
| **P2** offset paging | **done**, with caps | `portal/scraper.py: fetch()` |
| **P2** the handshake button | **done** | `POST /api/clients/{cid}/sources/{sid}/test` |
| **P2** local-hour scheduler | **done** | `PORTAL_SYNC_AT=03:30` |
| **P2** stale-`last_ok_at` warning | **done** | Admin → Clients, amber at 26 h, red at 36 h |
| **P3** surface `quotes` and `bookmarks` | **done**, chartable | `queries.trend`, both metric pickers |

### The three that matter most, in detail

**History.** `post_metric_days` is `PRIMARY KEY (client_id, post_key, day)`,
where `day` is the **pull** day in the client's timezone and `post_key` is your
`tweet_id` when you send one, else the normalised URL. One row per post per
pull. A second pull the same day replaces that day's row; a pull the next day
adds one. `last_refresh_ms` and `refresh_count` ride along, so a flat count is
distinguishable from a stale read months later, exactly as your §3.5 asked.

**One row, never all of them.** `_fold_scraper` now touches exactly one
`post_metrics` row per post: the one whose `sheet_date` equals your `day`; if
there is no row for that day, the single row that exists; if several, the most
recent. Never a loop over every day the URL has ever appeared. The test that
pins this is `TestCollector.test_only_the_posts_own_day_is_touched` — it seeds
the same post on two days, folds a fresh count for the later one, and asserts
the earlier day is **untouched**.

**The window, dropped where it should be and kept where it should be.** The
`[today-3, today]` check now applies only when the day was *guessed* from
`posted_at` — i.e. a legacy scraper with no `day` field. Any row carrying your
`day` is accepted whatever its date, so all 51 dated tabs back to 21 July
import on the first pull. `normalize()` records which case it was
(`day_given`), and `TestEnvelopeAndPaging.test_a_stated_day_beats_the_posts_publish_time`
pins it. Nothing has to be configured per source.

**Paging.** We set `limit` and `offset` on your URL, replacing any already
there — so `?project=16&limit=200` makes the page size 200 and we take it from
there; absent, 500. We walk until `total` is reached, and stop early on: an
empty page, a page that repeats what we already hold, `total` changing under
us, 50 pages, 25 000 rows, or 60 MB. A short read is **kept, not discarded** —
every post we did get now has current numbers, and a post nobody sent is simply
left alone, so a torn read costs a few updates rather than the day.

---

## 2. Answers to your open questions

**Q5b — `PORTAL_KEY_SECRET` is set.** Checked on the server's `.env`: present,
64 characters, as is `PORTAL_SESSION_SECRET`. The silent-skip you were worried
about cannot happen. **Issue the key.**

**Q5a — agreed, and thank you for refusing.** A project-locked key, not
Watch-Tower's. Your reasoning is the same as ours: the `403 "locked to project
N"` check is the only thing standing between a mis-typed project number and one
client's posts being filed under another, and an unscoped key cannot fire it.
We have wired the handshake so that check is exercised *before* any data moves.

**Q4 — 24 h, agreed.** We are configuring `PORTAL_SYNC_AT=03:30` (new; add
that one line to `.env` at deploy — it is already in `.env.example`). With it
set the sync runs once a day at 03:30 **Asia/Kolkata**, and a restart at 03:31
still catches that day. Your §5.1 is right that pulling more often than you
refresh buys nothing, and with `lag_days = 2` the client could not see the
difference anyway.

**`refresh_in_progress` — honoured.** Set a source's handshake URL and each
sync probes `/api/project` first; when the flag is true the source is skipped
for that run, logged as skipped rather than as an error, and taken tomorrow. It
never snapshots a half-scraped watchlist. This is why the handshake URL is
stored per source (`client_sources.probe_url`) and not only used by the button.

**Q2 — archive tabs: agreed, skip.** We will ask the operator to confirm
nothing lives only in `Tweet LInks`, `Counter Links`, `3rd Party Posting`.
One request about this in §4 below.

**Q3 — sheet-binding write: we are not building it yet.** Your §2.3 makes the
case honestly and we agree with your own framing: it saves one paste per
campaign, ever. That is real but small, and it is the only thing that would
turn a read-only boundary into a write one. We would rather spend the next
change on the FB/IG rows actually arriving. Your endpoint is built and costs
you nothing to leave in place; if Tilak wants the button, it is a small change
on our side and nothing on yours. **Tilak's call, not urgent.**

**Q1 / your Part 8 — FB and IG: the tool is ready for those rows now.**
Nothing needs to change here when they start arriving:

- `platform` is **read, not inferred**. `normalize()` believes a stated
  platform, and a row stating a platform we do not model is **skipped, not
  filed** — so the 44 YouTube links are safe to send if they ever leak through;
  they will be dropped, not charted as X. This closes the hazard you quoted
  from our §7. Test: `test_a_platform_we_do_not_model_is_skipped_not_guessed`.
- `tweet_id` carrying an IG media pk or an FB post id is fine — we store it as
  `post_metrics.tweet_id TEXT` and dedupe on it first, URL second. **No need to
  add `post_id`**; do not rename.
- `retweet_count` / `reply_count` keep their names and land in `shares` /
  `comments`; the browser already relabels per platform.
- `pending` for weeks is understood and handled — see §3.

---

## 3. Your §5.3 decision, made and written down

You asked us to state the precedence between the operator's typed columns and
your scraped numbers, explicitly, and to keep the loser. Done, in code:

```python
# webapp/portal_publish.py
_SCRAPER_WINS = ("x",)
```

- **X → the scraped number wins.** We read it off X itself; the typed cell is a
  human's copy of it.
- **Facebook / Instagram / anything else → the typed sheet number wins** while
  `metric_source == 'sheet'`, because until your Part 8 ships it is the only
  measurement that exists.
- **The loser is kept, never discarded.** Every fold writes
  `raw_metrics.scraper = {counts…, signature, last_refresh_ms, applied: true|false}`.
  So when the two disagree, the comparison is there in the row and an operator's
  typo is findable — rather than being noticed months later because a chart
  looks wrong.

**One line to change when your IG/FB engine is real:** add `"instagram"` and
`"facebook"` to `_SCRAPER_WINS`. Tell us when the counters are trustworthy and
we will flip it — we would rather flip it late than have a half-backfilled
engine overwrite numbers a human checked.

**`pending` does not read as broken.** A `pending` row is stored `status='ok'`
with NULL counters, and NULL renders as `—`, the same as any count a platform
does not show. It is "listed, not yet measured", which is what it means. Test:
`test_pending_is_shown_with_no_numbers`.

---

## 4. Verified live against project 16 — 10 Sep 2026

We have the key and have run the real contract end to end. **It works.** Both
calls made with the exact headers `fetch_raw()` sends.

| Check | Result |
|---|---|
| `GET /api/project?project=16` | **200** · project 16 “Varanasi Client” |
| `GET /api/links?project=16` | **200** · `total` 1614 |
| Envelope | carries **both** `rows` and `items` ✅ |
| Paging | `offset` 0 / 500 / 1500 → 500 / 500 / 114 rows, **no overlap**, sums to 1614 ✅ |
| `tweet_id` | a quoted **string** — `"2096598745530122459"` ✅ |
| `day` | present on **every** row, no nulls, spanning 2026-08-07 … 2026-09-09 ✅ |
| `group` | present, verbatim (`National X Influencers`, `Counter Comments Links`) ✅ |
| Counters | 440 of 500 `null` (unfetched); real `0`s only where a real zero exists — `view_count` never 0, `quote_count` 0 on 58 rows ✅ |
| `media[0].thumbnail_url` | present ✅ |
| `author_avatar`, `author_followers`, `lang`, `last_refresh_ms`, `refresh_count` | all present and mapped ✅ |

Every one of those field names is now pinned in our test suite
(`TestRealCollectorRow`), copied from your live response. If one is ever
renamed our build fails, instead of a count quietly vanishing from a client's
dashboard.

### Four things we saw that you should confirm

1. **`links` is 1614, not 2288**, because `paused: {watchlists: 3, links: 674}`
   — and 1614 + 674 = 2288 exactly. So the three archive tabs are paused as
   agreed in your Q2. Please confirm that is the whole of it and **no dated day
   tab is among the paused three**, since a paused list simply stops appearing
   and we would have no way to tell a paused day from an empty one.
2. **`refresh_in_progress` is `true` and only 306 of 1614 links are `ok`** (19%).
   The first backfill is clearly still running, and our scheduler will correctly
   skip every pull until it goes false. Roughly when does it settle — and does
   it return to `false` between daily passes, or stay `true` while any link is
   due? If the latter, we will never pull, and we would rather change our rule
   than discover that in a fortnight.
3. **`skipped_non_x` is not in the live handshake body.** Your §3.3 promised it
   and our admin page already renders it — it is the number an operator needs
   when a client asks why a third of the sheet is missing. Please add it.
4. **`tab_mode` comes back as `"all"`**, where your §3.3 example said `"dated"`.
   Cosmetic — we only display it — but worth aligning so the doc and the wire
   agree. (`sheet_title` also has a trailing space: `"Varansi Day Wise Data "`.)

## 5. What we still need from you

1. **`day_inferred: true`** — still the one real gap, and now confirmed absent
   on the wire. Detail below; it is the only field we are asking you to add.
   On any row whose `day` came from your `added_at` fallback rather than a tab
   title. Your §11.4 tells us to treat those rows with suspicion in a day-wise
   view and we agree — but the only signal today is prose inside `status_note`,
   and matching on an English sentence is exactly the kind of thing that breaks
   silently when the wording changes. A boolean lets us file them, flag them in
   the admin UI, and keep them out of the trend charts. Additive, so it costs
   Watch-Tower nothing.
2. **Paging is confirmed working** — nothing needed. Recorded here so it stays
   load-bearing on both sides: `limit`/`offset` as query parameters, `total` in
   the envelope, order stable across pages. We walk four pages at 1,614 links.

## Operator actions on the sheet (neither of us can do these)

- **Rename tab `24/7/25` → `24/7/26`.** Agreed with your §1.4: we are not
  guessing at a year either. As it stands that day files into July 2025 where
  no report will look.
- **Share the sheet with the service account as Viewer, and add
  `ganbjhs@gmail.com` as a Viewer too** (your §9.1b). The second half is the
  one that gets forgotten: access today rests entirely on `anyone: reader`, so
  turning the public link off without a named grant locks out every route at
  once, including the human ones.
- **Confirm the three archive tabs stay paused**, and that nothing lives only
  in them.

## Your Part 6 — day one

Agreed, and we will say it in those words to the client: 51 days of structure,
one snapshot of totals, zero days of growth. The trend charts stay out of the
first demo rather than showing a flat line someone reads as a dead campaign.
From the first nightly pull after deploy, `post_metric_days` gains one row per
post per day; the Growth view is genuine after about a fortnight.

## One thing from our side you should know

Fixing the migration surfaced a **pre-existing boot crash** unrelated to this
integration: `portal/schema.py: ensure_schema()` ran `CREATE UNIQUE INDEX …
(lower(username))` inside the DDL script, so on any `portal.db` created before
the `username` column existed, `executescript()` raised and **both apps failed
to start** — on exactly the deployments with data worth keeping. Our local
`portal.db` (340 posts) is in that state and reproduced it. Both index
creations now run after the `ALTER`s. Your rule generalises well and we have
adopted it: *a compatible-looking success from an older peer is more dangerous
than an error* — so is a schema step that only ever runs on a fresh database.

# VedicReport — Client Portal plan

> **Status 6 Sep 2026: P0 + P1 + most of P2 built** — `portal/` (the app), `webapp/portal_publish.py` (publish + scraper sync + backfill), `webapp/routes_clients.py` + `templates/clients.html` (Admin → Clients), `scripts/portal_backfill.py`, `Dockerfile.portal`, compose + Caddy entries, 14 zero-network tests. How to run and deploy: [`portal/README.md`](../portal/README.md). Not yet built: screenshots for clients (`/media` exists, the copy step is wired but off by default), run PDFs for download, per-client subdomains, e-mail digests.

*Draft v1, 5 Sep 2026. Companion prototype: `docs/client-dashboard-demo.html` (also published as the "Varanasi Social Pulse" artifact). Click through it first — this file explains what you saw and how it gets built without touching what colleagues use today.*

---

## 0. What this is, in one paragraph

Report Maker stays the **internal tool**: projects, styles, runs, sources, bots — everything under `report.vedictech.in`, for colleagues only. The **Client Portal** is a second, separate web app on its own hostname (`clients.vedictech.in`) that a client signs in to and sees *only their own* numbers: charts per category and platform, a feed of every post, month-against-month comparison, and an Excel export. It never reads the Google Sheet, never runs a browser, never opens `data/jobs/`. It reads one table — `post_metrics` — that the internal tool **publishes into** after each run, with a two-day delay baked in as a column. That one-way publish is the isolation boundary: a client can only ever see what was deliberately published for them, and nothing about how it was made.

---

## 1. What the sheet actually contains (read 5 Sep 2026)

Workbook **"Varansi Day Wise Data"**, 49 tabs:

| Tab | Shape |
|---|---|
| `Tweet LInks` (gid 0) | col A date headings (`Date- 4-7-26 Sr No.` … `21-7-26`), col B links, col C an unnamed number on the first day only. Legacy — stopped 21 Jul. |
| `Counter Links`, `3rd Party Posting` | older per-purpose tabs |
| `21/7/26` … `4/9/26` (46 tabs) | **one tab per day, one column**: a heading row, then links. Headings seen: *3 Party Pages Posting · Hyper Local Pages Posting · National X Influencers · Counter Comments Links*. Links are FB reels/posts, IG reels, X statuses, mixed under each heading. |

Two consequences:

1. **The sheet has no metrics.** Likes / comments / shares / views come from the run — `metrics/x_metrics.py` (live X page) and `metrics/shot_metrics.py` (OCR off the screenshot), switched on by the project's *Read engagement numbers from the posts* setting — and land per post in the run's `results.json` (`metrics` dict, plus `handle`, `platform`, `category`, `post_link`, `status`). So the client dashboard's data source is **the run output, not the sheet.** The sheet only contributes the category (heading) and the date (tab name), and `smartsheet.py` already extracts both.
2. **Category = the heading text**, exactly as typed. The portal needs a per-client `category_map` to rename ("3 Party Pages Posting" → "3rd Party Pages"), order, and hide categories (*Counter Comments Links* is arguably internal ops — see open question 2).

Numbers that are not public are not in the picture and must stay blank, never 0: IG shares, FB photo-post views, X bookmarks. The prototype shows these as "—" with a tooltip; the fact table keeps them `NULL`.

---

## 2. Isolation — the three ways, and the one to take

| | A · Separate app, separate host, separate DB (**recommended**) | B · Router in the same FastAPI app (`/client/*`) | C · Separate repo + Postgres + ETL |
|---|---|---|---|
| Cookies / sessions | own cookie name, own secret, own domain → an internal session can never be replayed at the portal, and vice-versa | shares `SessionMiddleware`, same cookie, same domain — one role bug leaks the internal tool | own |
| Static / templates | own `portal/static`, `portal/templates`; nothing imported from `webapp/` | shares `app.css` / `app.js`; client sees internal styles, hidden routes are one URL guess away | own |
| Data | reads `data/portal.db` only; internal app writes into it at publish time | queries `jobs`, `results.json` directly — every query needs its own tenant filter | ETL pipeline to maintain |
| Blast radius of a bug | portal only | whole tool | portal only |
| Effort to first client | ~2 weeks | ~1 week | ~4+ weeks |
| Run it as | one more container in `docker-compose.yml`, one more Caddy block | nothing new | new infra |

**Take A.** It is the smallest thing that is actually isolated. B is faster for a week and then costs every week after (every new internal feature must be re-checked against client exposure). C is where A grows into if there are ever dozens of clients or SSO requirements — the fact table designed below moves to Postgres unchanged.

Three rules that make A hold:

* **Publish is the only bridge.** `webapp/` imports nothing from `portal/`; `portal/` imports nothing from `webapp/`. A tiny `portal_publish.py` in `webapp/` writes rows into `data/portal.db`. That is the whole coupling.
* **The portal opens its data read-only** (`sqlite3.connect("file:data/portal.db?mode=ro", uri=True)`) for everything except its own auth tables (`client_users`, `client_sessions`, `client_login_attempts`, `portal_audit`), which live in the same file but are the portal's alone.
* **One function decides visibility** — `visible_posts(client_id)` returns `post_metrics WHERE client_id = ? AND visible_from <= today_ist`. No route touches `post_metrics` any other way (the same "one place the permission rule lives" discipline `api_v1.py` already follows). `client_id` comes from the session, never from a URL, query string or form.

---

## 3. Data model — `data/portal.db`

```
clients
  id, slug, name, display_name, logo_path, accent_hex, lag_days (default 2),
  tz (default Asia/Kolkata), category_map (JSON: {heading: {label, order, hidden}}),
  show_screenshots (0/1), show_reports (0/1), created_at, archived

client_projects            -- which internal projects feed which client
  client_id, project_id, PRIMARY KEY (client_id, project_id)

client_users
  id, client_id, email, pw_hash (pbkdf2_sha256 — same helper as webapp/auth.py, copied not imported),
  role ('viewer' | 'manager'), invited_by, invite_token, invite_expires, last_login_at, disabled

client_sessions            -- server-side sessions: revocable, no state in the cookie
  sid, client_user_id, created_at, last_seen_at, ip, ua

client_login_attempts      -- same shape as login_attempts in jobs.db
portal_audit               -- login, logout, export, failed login: who / when / what / client_id

post_metrics               -- THE fact table. One row per (client, post, sheet day).
  id
  client_id, project_id, run_id
  sheet_date        DATE   -- the day tab (3/9/26 → 2026-09-03); the date the client sees
  visible_from      DATE   -- sheet_date + clients.lag_days, computed at publish
  captured_at       TS     -- when the run actually read the numbers
  platform          ('x' | 'facebook' | 'instagram')
  category_raw      TEXT   -- heading exactly as in the sheet
  handle            TEXT
  post_url          TEXT
  post_url_norm     TEXT   -- tracking params stripped, host canonical (links.py already does this)
  post_type         ('post' | 'reel' | 'photo' | 'video' | NULL)
  likes, comments, shares, views, reach, impressions   INTEGER NULL   -- NULL = not public / not read, never 0
  metric_source     ('sheet' | 'page' | 'ocr' | 'mixed' | 'none')
  raw_metrics       JSON   -- the metrics dict from results.json, verbatim, incl. "1.1K"-style strings
  status            ('ok' | 'skipped'), skip_reason TEXT
  screenshot_path   TEXT NULL   -- only set if client.show_screenshots; copied under data/portal/media/<client>/
  published_at      TS, first_published_at TS
  UNIQUE (client_id, post_url_norm, sheet_date)

run_outputs (optional, P3)  -- lets a client download the PDF/PPTX the run already built
  client_id, run_id, sheet_date, kind, filename, path, bytes, visible_from
```

Indexes: `(client_id, visible_from)`, `(client_id, sheet_date, category_raw, platform)`, `(client_id, post_url_norm)`.

Why `visible_from` is a column and not a `WHERE sheet_date <= today-2` at query time: the lag becomes per-client data, is trivially auditable (`SELECT … WHERE visible_from > today` shows exactly what is being held back), and a later change to a client's `lag_days` re-publishes forward without touching query code.

Why `sheet_date`, not `captured_at`, is the client's date: the team's unit of work is the day tab. A 3/9 tab captured on 4/9 evening is "3 Sep" to the client. Metrics are then "as read at capture" — a caveat the footer states (prototype footer text).

---

## 4. The publish step (in the internal tool)

`webapp/portal_publish.py`, ~150 lines:

```
publish_run(job_id):
    job   = store.get(job_id)                      # has project_id, created_at, title
    clients = client_projects for job.project_id   # zero → return (not a client project)
    results = runner._read_results(job dir)        # the per-post list
    sheet_date = job.sheet_date or date(job.created_at)    # see below
    for each client:
        for r in results:
            row = normalise(r)                     # platform, handle, url_norm, ints from raw_metrics
            row.visible_from = sheet_date + client.lag_days
            upsert into post_metrics on (client_id, post_url_norm, sheet_date)   # metrics may grow; keep latest, keep first_published_at
            if client.show_screenshots and r.screenshot: copy to data/portal/media/<client>/<run>/…
    log to job activity: "Published 53 posts to Varanasi Campaign (visible from 5 Sep)"
```

Wire-up, three touches:

1. **`webapp/jobs/runner.py`** — at the point a job is marked `done`, call `portal_publish.publish_run(job_id)` inside a try/except that logs and never fails the run (RULEBOOK rule 17: say so, keep going).
2. **`sources.py`** — the source check already knows the tab it ran from (`u["latest_date"]`, `u["tab"]["name"]`). Pass it into `runs.create_run(... sheet_date=…)` and add a `sheet_date` column to `jobs` (nullable, `ALTER TABLE` via the existing `_COLUMNS` mechanism in `store.py`). A run started by hand from the New run page gets today's date unless the sheet reader found one.
3. **`scripts/portal_backfill.py`** — one-off: walk every finished job of a client's projects, read `results.json`, publish. This is how 21 Jul → today lands in the portal on day one without a single re-capture.

Parsing OCR strings to integers: `shot_metrics.py` already produces both the shown text and the parsed number (`metrics/README.md`). Store the int in the typed column, the text in `raw_metrics`. `"hidden"` (the Kashi deck's *missing* word) → `NULL`.

**What is never published:** job logs, capture status internals beyond `ok/skipped + reason`, session state, style JSON, screenshots unless the client flag is on, anything from projects not linked to the client.

---

## 5. Admin side (in the internal tool) — `Admin → Clients`

One page, admin-only (`auth.require_admin`), under `/admin/clients`:

* **Create client**: name, slug (→ subdomain later), logo (PNG/SVG ≤ 200 KB), accent colour, lag days (default 2), timezone, show screenshots / show reports.
* **Link projects**: multi-select of projects. Linking triggers a backfill of that project's finished runs.
* **Categories**: table of every `category_raw` seen for this client's projects → client label, order, hidden. Defaults: label = raw, order = first-seen.
* **Data source**: the scraper for this client — signature name, base URL, API key (write-only field; shows *set on <date> by <user>* afterwards), *Test* button that fetches yesterday and reports the post count. See §7b.
* **Users**: invite by email → invite link (token, 72 h). No passwords typed by admins. Disable / reset.
* **Publish log**: last 50 publishes (run, posts, visible-from), "Re-publish" button for a run.
* **Preview as client**: opens the portal in a new tab with a 10-minute impersonation token *(P2 — useful for support, must be audited)*.

---

## 6. The portal — `portal/`

Same conventions as `webapp/`: FastAPI, Jinja2, one CSS file, one JS file, no build step. The prototype is already written that way (single file); it splits into `templates/dashboard.html` + `static/portal.css` + `static/portal.js` with the in-memory arrays replaced by `fetch()`.

```
portal/
  main.py          app, session middleware (cookie "portal_sid", PORTAL_SESSION_SECRET), security headers, /healthz
  config.py        PORTAL_DB, PORTAL_HOST, PORTAL_SESSION_SECRET, PORTAL_LAG_DAYS_DEFAULT=2, PORTAL_TZ
  auth.py          invite accept → set password; login (rate-limited like webapp/auth.py); logout; server-side sessions
  db.py            read-only connection + visible_posts(client_id) — the only reader of post_metrics
  queries.py       summary / daily / posts / top / monthly aggregates (SQL, GROUP BY)
  export.py        openpyxl workbook: Summary · Posts · Daily · Monthly (same sheets as the prototype)
  routes.py        pages + JSON API below
  templates/       login.html, dashboard.html, error.html
  static/          portal.css, portal.js, fonts (self-hosted, no Google Fonts call from a client page)
```

**Routes** (all session-authenticated; `client_id` from the session):

| Route | Returns | Feeds |
|---|---|---|
| `GET /` | the dashboard page with `meta` inlined | — |
| `GET /api/meta` | client branding, categories (mapped, ordered, hidden removed), platforms, `data_through`, `data_from`, `lag_days` | header pill, range limits, filter options |
| `GET /api/daily?day=YYYY-MM-DD` | every visible post of that day — category, platform, name, handle, avatar, caption, media, times, counts, url — merged from `post_metrics` (sheet: day + category) and the scraper cache (content + counts) | Today hero, top posts **and** the feed — one call |
| `GET /api/trend?from&to` | per day × category × platform: posts, likes, comments, shares, views (aggregates only, no post content) — `to` clamped to `data_through` | every category graph and the Growth chart — one call |
| `GET /api/export.xlsx?day=&from=&to=` | streamed workbook (Day · Day summary · Trend · Growth), audit-logged | Export button |
| `GET /media/{token}` | screenshot, only if `show_screenshots`, path validated inside the client's media dir | feed thumbnails (P3) |

Every query clamps `to ≤ data_through` (= max `visible_from ≤ today`) server-side. A `from` before the client's first visible row is clamped too — the prototype's "the sheet starts 21 Jul" note is that clamp made visible.

**The two-day rule, stated once**: `today_ist = now(Asia/Kolkata).date()`; a row is visible when `visible_from <= today_ist`. Not "48 hours since capture" — calendar days in the client's timezone, so the pill can say "Data through Thu 3 Sep" and be exactly right at 00:00 IST.

**Timezone**: dates are stored as ISO strings and compared as strings; the only clock read is `today_ist`. No UTC-midnight surprises.

---

## 7. What the dashboard shows (prototype v3 → spec)

*Revised 6 Sep after two rounds of review. The page is anchored on a **report day** (today − 2) and has four parts: Today · Categories · Growth · Feed.*

**Engagement** = likes + comments (replies on X) + shares (reposts on X). Views are shown beside it, never inside it. Not-public counts (IG shares, FB photo-post views) print as "—", never 0.

| Block | What it shows | Rule / data |
|---|---|---|
| **Report day** picker (‹ date ›) in the sticky bar — opens on **today − 2** | drives every block below | `max` = `data_through` from `/api/meta` (newest day whose `visible_from <= today_ist`); `min` = first published day |
| **Today** — hero: the date, a one-line "published 2 days after posting" note, three KPI tiles (Engagement · Views · Posts) each with Δ vs the day before and a 7-day sparkline; right side: share-of-engagement bar by platform and a platform table (posts, views, engagement, share %, Δ vs yesterday) | the day at a glance | `/api/daily?day=` for the day and the 7 days before it |
| **Today** — *Top posts of the day*: one card per platform — media, name/@handle, caption, category, engagement / likes / comments / views | the best post on each platform | same call; the whole card links to the post |
| **Categories** — a **parent filter bar**: Duration (7 · 14 · 30 days · custom from→to, ending on the report day) and Metric (Engagement · Likes · Comments/replies · Shares/reposts · Views); then **one block per sheet heading**, each with **three trend graphs — Facebook, Instagram, X** | *metric per day* as an area-line trend, peak and last day marked, crosshair tooltip with metric · posts · per-post; per panel: total for the duration, Δ per day vs the previous equal duration, posts, avg per post | `/api/trend?from&to` → per day × category × platform: posts, likes, comments, shares, views — one call feeds all 12 graphs. X-axis is time; metrics are on the axis; **no handle names** in the graphs (handles belong to the feed) |
| **Growth** — Duration (7 · 14 · 30 · 60 days) · Metric · Split by platform / by category | one multi-line trend graph, headline total with Δ per day vs the previous period, legend, and a per-series strip (total, Δ, posts, per day, per post) | same `/api/trend` data; replaces the earlier month-vs-month tables |
| **Feed** — cards: avatar, name, @handle, platform pill, caption, media thumbnail (image / video) on the right, *collected / posted* ages, ❤ 🔁 💬 👁, *Open on …*, *Copy link*, category chip | every post of the report day | from the scraper (§7b); filters: category, platform, sort, search; paged 20 |
| **Export to Excel** | *Day* (one row per post incl. caption and link) · *Day summary* (category × platform) · *Trend* (every day of the chosen duration, all metrics) · *Growth* | server-side `openpyxl`, audit-logged |
| **Data source** panel (header button) | signature name · API URL with `{from}` `{to}` (`{date}` = report day) · API key (password field, stored once, never echoed) · *Save & fetch*; or paste the scraper's JSON | `FIELD_MAP` at the top of the script — edit it once against a real response |

Colours: platform hues are fixed (Facebook blue, Instagram orange, X aqua — validated for colour-blind separation) and carry through the share bar, the category graphs and the growth lines; categories in the "by category" growth split use the next validated slots. Per-client branding = the accent CSS variables (hero wash, KPI sparklines, buttons) + logo + display name, injected by the template. Duration presets are placeholders — the client's own list is a one-line change.

### 7b. The scraper as the data source

The client's numbers and post content come from **our own scraper**, which returns JSON per post. Two rules:

1. **The API key never reaches a browser.** In the prototype the key is typed into the page and kept in that browser's local storage only — fine for a demo, wrong for production. In the portal the key is a row in `client_sources` (`client_id, signature_name, base_url, api_key_enc, added_by, added_at, last_ok_at, last_error`), entered once in **Admin → Clients → Data source**, stored encrypted with a server-side secret, and used only by the server. The client's browser talks to `/api/*` on the portal, which talks to the scraper.
2. **Signature name** = the label for that key (shown in the header, written into the audit log and sent as `X-Signature-Name` on every call), so a rotated or revoked key is traceable.

Fetch shape the portal expects (adapter in `portal/scraper.py`, mirrors `FIELD_MAP` in the prototype): `GET {base_url}?date=YYYY-MM-DD` with `Authorization: Bearer <key>` → JSON array (or `{"posts":[…]}`) of posts carrying platform, username / name / avatar, caption, media (type + thumbnail), posted_at, collected_at, url, like / comment / share / view counts, and a category. Unknown field names are mapped in one place; a post without a category falls back to the sheet's heading for that link (`post_metrics.category_raw`), which is why the publish step (§4) still runs — the sheet remains the source of truth for *which* links belong to *which* category on *which* day, and the scraper supplies everything else.

The portal caches each day's scraper response (`scraper_cache: client_id, day, fetched_at, json`) so a client reloading the page does not re-hit the scraper, and a day older than `lag_days` is refreshed at most once a day.

---

## 8. Deployment

```yaml
# docker-compose.yml (additions)
portal:
  build: { context: ., dockerfile: Dockerfile.portal }   # python:3.12-slim + fastapi + openpyxl; NO chromium, NO tesseract
  environment: [PORTAL_DB=/app/data/portal.db, PORTAL_SESSION_SECRET, PORTAL_HOST=clients.vedictech.in, PORTAL_TZ=Asia/Kolkata]
  volumes:
    - ./data/portal.db:/app/data/portal.db
    - ./data/portal/media:/app/data/portal/media:ro
  ports: ["127.0.0.1:8020:8020"]
  restart: unless-stopped
```

```caddy
clients.vedictech.in {
    encode gzip
    header {
        Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"
        X-Frame-Options DENY
        Referrer-Policy no-referrer
        X-Robots-Tag "noindex, nofollow"
    }
    reverse_proxy portal:8020
}
```

DNS: one A record for `clients.vedictech.in` → same server. Later, `*.vedictech.in` wildcard + Caddy on-demand TLS gives `<client-slug>.vedictech.in` with the same app reading the slug from the host.

The portal image has no browser and no OCR — it is a 150 MB Python image that cannot capture anything even if compromised.

---

## 9. Security checklist (before the first client logs in)

- [ ] `PORTAL_SESSION_SECRET` ≠ `SESSION_SECRET`; cookie name `portal_sid`; `Secure; HttpOnly; SameSite=Lax`
- [ ] Server-side sessions (revocable); idle timeout 24 h; "sign out everywhere" on password change
- [ ] `client_id` only ever read from the session — grep the routes for any `client` query param and delete it
- [ ] IDOR test: user of client A requests `/api/posts` with B's filters → sees only A; `/media/<B token>` → 404
- [ ] Login rate limit (reuse the `login_attempts` approach), generic error text, constant-time compare
- [ ] Invite tokens single-use, 72 h; password min 10 chars; pbkdf2 240k iterations
- [ ] Export audited (who, when, range, rows) — clients will ask "who downloaded this"
- [ ] No Google Fonts / CDN calls from the portal page (self-host the two fonts; CSP above enforces it)
- [ ] `robots` noindex; `/healthz` returns no version string
- [ ] Nightly `sqlite3 data/portal.db ".backup …"` alongside the existing backup script in `handover/`
- [ ] Nothing under `webapp/static` or `data/jobs` is reachable from the portal container (volumes above)

---

## 10. Phasing (each step shippable alone)

| Phase | Scope | Days |
|---|---|---|
| **P0 — Publish** | `portal.db` schema · `portal_publish.py` · `sheet_date` on jobs · hook in `runner.py` · `scripts/portal_backfill.py` · `Admin → Clients` (create, link projects, invite) | 3 |
| **P1 — Portal** | `portal/` app: invite → login · `/api/meta`, `/api/daily`, `/api/posts`, `/api/top` · dashboard page from the prototype (Overview, By category, Feed) · compose + Caddy + DNS | 5 |
| **P2 — Compare & export** | `/api/monthly` · `/api/export.xlsx` (openpyxl) · per-client branding + category map · export audit · "Preview as client" | 3 |
| **P3 — Extras** | screenshots opt-in (`/media`) · run PDFs for download (`run_outputs`) · weekly email digest · per-client subdomains · client API key (read-only, same `visible_posts`) | as asked |

Acceptance for P1 (rule 3 — open it and look): sign in as a test client, confirm the pill says *Data through <today−2>*, confirm a post from *yesterday's* tab is **absent** from feed, charts and export, then wait a day and confirm it appears.

---

## 11. Decisions to take (open questions)

1. **Show the run's PDF/PPTX to clients?** They already exist per run; it is a `run_outputs` row and a download route with the same `visible_from`. Cheap, and probably what the client asks for first. *Recommendation: yes, P3, behind `show_reports`.*
2. **Is "Counter Comments Links" a client category?** It reads like internal ops. *Recommendation: hidden by default in `category_map`; the admin flips it per client.*
3. **Metric refresh.** Numbers are read once at capture (the day after posting). Likes keep growing for days. Either accept "as read on capture day" (state it in the footer, as the prototype does), or schedule a weekly re-read of the last 7 days (`x_metrics.py` only, no screenshots — cheap for X; FB/IG need a screenshot to OCR, so not cheap). *Recommendation: accept for v1; revisit if a client asks why numbers differ from the platform.*
4. **Lag: 2 calendar days after the sheet date, IST.** Confirm this is what "2 days before data" means to the client, not 48 h after capture.
5. **One client = one project, or many?** Schema allows many-to-many; UI in P0 links projects to a client. *Recommendation: keep it many; "Varanasi Campaign" may get a second sheet.*

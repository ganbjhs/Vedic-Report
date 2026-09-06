# Client Portal — Blueprint, Features & Manual (handover)

> **Purpose of this doc:** everything needed to continue building the Client Portal in a fresh chat, plus the operational fixes made to the production server on 6 Sep 2026. Companion files in the repo: `docs/client-portal-plan.md` (full design rationale), `portal/README.md` (run/deploy), `docs/client-dashboard-demo.html` (the interactive prototype).

---

## 0. One-paragraph summary

VedicReport is an **internal** tool the team uses to capture X/Facebook/Instagram posts from a Google Sheet and build PDF/DOCX reports. The **Client Portal** is a **second, separate web app** (`portal/`) that a client logs into to see *their own* numbers as a **daily social report** — one day at a time, published with a **2-day delay** — with trend graphs per category and platform, a growth trend, a feed of the day's posts, and an Excel export. The two apps share exactly one thing: a SQLite file `data/portal.db` that the internal tool **writes** and the portal **reads**. That one-way publish is the isolation boundary — a client can only ever see what was deliberately published for them, and nothing about how it was made.

---

## 1. Status — what is built vs pending

**Built and tested (14 zero-network tests pass):**
- `portal/` — the client-facing FastAPI app: login, invite links, server-side sessions, the dashboard (Today · Categories · Growth · Feed), Excel export, security headers, read-only DB access, the two-day visibility rule, tenant isolation.
- `webapp/portal_publish.py` — the bridge: `publish_run()` (after each report run), `sync_client()` (pull the scraper), `backfill_client()`, and a scraper-sync scheduler.
- `webapp/routes_clients.py` + `webapp/templates/clients.html` — **Admin → Clients** inside the internal tool: create clients, link projects, rename/hide categories, invite users, add the scraper API key (sealed), sync now, publish log, audit.
- `scripts/portal_backfill.py` — publish every finished run from the command line.
- `Dockerfile.portal`, `requirements-portal.txt`, compose + Caddy entries, `portal/tests/test_portal.py`.

**Not yet built (the "next" list):**
1. **Client screenshots** — `/media/<id>` route exists and the copy step is wired, but it's **off by default** (`show_screenshots=0`). Turn on per client; verify the media path.
2. **Run PDFs for download** — let a client download the actual report PDF/PPTX the run built (`run_outputs` table sketched in the plan, not implemented).
3. **Per-client subdomains** — `clients.vedictech.in` serves one portal; wildcard `*.vedictech.in` + read the client from the host is a later step.
4. **Email digests** — a weekly "your report is ready" email.
5. **Real scraper field mapping** — `portal/scraper.py: FIELD_MAP` is a best guess. **Needs one real JSON response from the team's scraper (key removed) to pin exactly.** This is the single most important input to get the feed content correct.
6. **Deploy** — the portal is only in the repo (local + now committed on server's feature branch as of the fix below). It has **not** been built/run as a container in production yet.

---

## 2. Architecture (how a number reaches a client)

```
INTERNAL TOOL (webapp/, report.vedictech.in)         CLIENT PORTAL (portal/, clients.vedictech.in)
────────────────────────────────────────────         ─────────────────────────────────────────────
A report run finishes
    └─ portal_publish.publish_run(job_id)
         reads the run's results.json + metrics
         writes one row per post into ───────────►  post_metrics  (data/portal.db)
         post_metrics, visible_from = sheet_date+lag        │
                                                            │  (read-only connection)
Admin → Clients                                             ▼
    · create client, set 2-day delay, accent, logo    GET /api/meta   → branding, categories, date bounds
    · link projects (auto-backfills history)           GET /api/daily?day=  → Today hero + top posts + feed
    · rename / order / hide the sheet's headings       GET /api/trend?from&to → every category graph + growth
    · invite client users (one-time links)             GET /api/export.xlsx → Day · Day summary · Trend · Growth
    · add the scraper API key (sealed)
         └─ sync_client() pulls the scraper ─────────►  fills caption, media, avatar, fresh counts
```

**The isolation rules (do not break these):**
- `portal/db.py: visible_posts()` is the **only** reader of `post_metrics`, and it *always* adds `client_id = <session's client> AND visible_from <= today(client tz)`. There is **no client id in any URL** — it comes from the session.
- The portal opens the DB `mode=ro` for everything except its own auth tables. A bug in a query cannot become a write.
- The portal imports **nothing** from `webapp/`; `webapp/portal_publish.py` imports only the portal's leaf modules (`schema`, `util`, `secretbox`, `scraper` — standard library only).

---

## 3. The dashboard — features (what the client sees)

Anchored on a **report day** = today − 2 (the "2-day delay"; figures settle before the client sees them). Four sections:

| Section | What it shows |
|---|---|
| **Today** (hero) | the date; 3 KPI tiles (Engagement · Views · Posts) each with Δ vs the day before and a 7-day sparkline; a share-of-engagement bar by platform; a platform table (posts, views, engagement, share %, Δ). Then **Top posts of the day** — one card per platform (media, handle, caption, category, engagement/likes/comments/views). |
| **Categories** | a **parent filter bar** — Duration (7/14/30 days · custom) and Metric (Engagement/Likes/Comments/Shares/Views). Then **one block per sheet heading**, each with **three trend graphs — Facebook, Instagram, X** — the metric per day as an area-line trend, with per-panel total, Δ vs the previous equal period, posts, and avg per post. **Metrics on the axis, no handle names** (handles belong to the feed). |
| **Growth** | a duration-based multi-line trend (7/14/30/60 days), metric picker, split **by platform** or **by category**; headline total with Δ per day vs the previous period; a per-series strip (total, Δ, posts, per day, per post). Replaces month-vs-month tables. |
| **Feed** | one **card** per post: avatar, name, @handle, platform pill, caption, media thumbnail (image/video), collected/posted ages, ❤ 🔁 💬 👁 counts, *Open on …*, *Copy link*, category chip. Filters: category, platform, sort, search; paged 20. |
| **Export** | Excel workbook: *Day* (one row per post incl. caption + link), *Day summary* (category × platform), *Trend* (every day of the duration), *Growth*. |

**Definitions the portal enforces:** Engagement = likes + comments + shares (replies/reposts on X). Views shown separately. Not-public counts (IG shares, FB photo views) print "—", never 0. Colours: Facebook blue, Instagram orange, X aqua (fixed, colour-blind-safe). Per-client branding = accent colour + logo + display name.

---

## 4. Data model (data/portal.db) — the tables that matter

- **`post_metrics`** — THE fact table. One row per (client, post, sheet day): `client_id, sheet_date, visible_from, platform, category_raw, handle, display_name, avatar_url, caption, media_type, thumb_url, posted_at, collected_at, likes, comments, shares, views, reach, metric_source, status, screenshot_path`. `visible_from = sheet_date + client.lag_days` (the two-day rule as data). Unique on `(client_id, post_url_norm, sheet_date)`.
- **`clients`** — `slug, name, display_name, logo_path, accent_hex, lag_days (2), tz (Asia/Kolkata), category_map (JSON: {raw heading: {label, order, hidden}}), show_screenshots, show_reports`.
- **`client_projects`** — which internal projects feed which client.
- **`client_sources`** — the scraper per client: `signature_name, base_url (with {from}{to}/{date}), api_key_enc (sealed), auth_style`.
- **`client_users`**, **`client_sessions`**, **`client_login_attempts`** — portal auth (invite → password → server-side session).
- **`scraper_cache`**, **`publish_log`**, **`portal_audit`** — the scraper's raw answers, what was published when, and who signed in / exported.

Full DDL: `portal/schema.py`.

---

## 5. How to run & test (local)

```bash
# internal tool (port 8000) and portal (port 8020) run side by side; both open data/portal.db
.venv/bin/pip install -r requirements-portal.txt
# .env needs: PORTAL_SESSION_SECRET, PORTAL_KEY_SECRET (two long random strings), PORTAL_PUBLIC_URL=http://127.0.0.1:8020
.venv/bin/python -m uvicorn portal.main:app --port 8020 --reload

# tests (no browser, no network)
.venv/bin/python portal/tests/test_portal.py
```

**First client, end to end:** Report Maker → **Admin → Clients → New client** (name, 2-day delay, accent) → tick its **projects** and Save (auto-backfills finished runs) → **Categories** (rename/hide the sheet headings, e.g. hide *Counter Comments*) → **Invite** an email (copy the one-time link) → optionally **Data source** (scraper URL + key, *Sync now*). CLI shortcut for the backfill: `python scripts/portal_backfill.py [--sync]`.

---

## 6. Deploy the portal (when ready)

```bash
# .env on the server — same PORTAL_KEY_SECRET for web and portal
PORTAL_PUBLIC_URL=https://clients.vedictech.in
PORTAL_SESSION_SECRET=…   PORTAL_KEY_SECRET=…   PORTAL_COOKIE_SECURE=1

docker compose up -d --build portal        # Caddyfile already routes clients.vedictech.in
```
DNS: an A record for `clients.vedictech.in` → the server. Caddy issues the cert on first start. The portal image (`Dockerfile.portal`) has **no browser and no OCR** — it only reads `portal.db`.

---

## 7. The one input still needed

`portal/scraper.py: FIELD_MAP` maps the scraper's JSON fields to the portal's fields (loosely — `like_count`, `likes`, `favorite_count` all work). It's a best guess. **Send one real post from the scraper's JSON with the key removed**, and pin the mapping exactly — then captions, media, avatars and counts in the feed will be correct. This is the top item for the next session.

---

## 8. To continue in a new chat — paste this

> "Continue the VedicReport **Client Portal**. Read `docs/CLIENT-PORTAL-BLUEPRINT.md`, `docs/client-portal-plan.md`, `portal/README.md`, and the `portal/` package. It's built and tested but not deployed. Next: (1) pin `portal/scraper.py: FIELD_MAP` to our real scraper JSON [paste one sample post, key removed], (2) deploy the portal container, (3) turn on client screenshots. Local repo: ~/Desktop/Project/VedicReport."

---

## 9. Production incident fixed today (6 Sep 2026) — for reference

**Symptom:** the daily Twitter report failed again and again ("Stalled", 16/52).
**Root cause:** the server is **1 vCPU / 3.8 GB**, but `.env` had `WORKERS=2` / `MAX_WORKERS=6`. Two Chromium browsers → memory over 3.8 GB → kernel **OOM-killed** a browser mid-capture (3× today) → Playwright's pipe broke (`write EPIPE`) → the other worker wedged → 20-min **stall** → fail. Every Resume repeated it.
**Fix (server `.env`, backed up as `.env.bak.*`):** `WORKERS=1`, `MAX_WORKERS=1` (one core = one browser). Verified: an 8-link and the full **52-link** report both completed cleanly (51/52; 1 skipped only because X age-restricted it), memory plateaus ~3.0 GB with ~0.9 GB free, no OOM. The finished report is at `/root/app/reports/5-9-26_Daily_Tweet_Report.pdf` (+ `.docx`).

**Follow-ups (recommended, not yet done):**
- Keep `WORKERS=1` on this box. Only raise it on a bigger server (2+ vCPU / 8 GB).
- Add `init: true` to the `web` service in `docker-compose.yml` so orphaned Chromium is reaped after any crash.
- Lower the 20-minute stall timeout so a wedged run fails fast.
- Rebuild the `web` image so the running Playwright matches the pinned `1.61.0` (the running image has `1.61.1-beta`; it works, so this is hygiene, not urgent).

**Git:** committed `5163b20` on `feat/splitter-and-v1` — pins `playwright==1.61.0`, gitignores runtime dirs; working tree clean, 1 commit ahead of origin. Push it with your own credentials (`git push origin feat/splitter-and-v1`) — it's a fast-forward.

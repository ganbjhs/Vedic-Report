# VedicReport v3 — "Projects + Grok" release plan

*Draft v3, 17 Aug 2026 — **status 18 Aug: 3.0-a shipped** (projects, project styles, PPTX for numeric styles, page background). Next: API panel + Sources. (project switcher in the left bar; project pages in the left bar; metrics optional; post metrics deferred; simple API; Grok chat per project). Companion demo: `docs/v3-dashboard-demo.html` (click through it first — this file explains what you saw).*

---

## 0. What v3 is, in one paragraph

Today Report Maker is a **form**: pick platform, pick style, drop links, download. v3 turns it into a **workspace of projects**. A project is a client or a recurring report ("Kashi Ke Wasi — monthly", "July fake accounts"): it owns its data sources (a Google Sheet that stays in sync, uploaded Excel, pasted links), the styles it prints in (one or more picked from a shared **style pool**), its run history, its **memory** (what Grok has learned about this client's report look and language), and its **interactive dashboard** (charts of what was captured, per section, per platform, per run). On top sit two Grok-powered features: **Grok Studio** — show it 2–10 sample reports and talk to it (text or voice) and it writes the report template for you — and **Enrich**, a one-click pass that adds insight text, tags, comparisons and charts to a finished report. Under all of it, the capture engine stops signing in to X, and everything that only existed to work around the old frozen pipeline (per-job code copies, one Chromium per worker process, server-side previews per keystroke, session warm-ups) is removed or replaced with something lighter.

My honest read of the plan: it is the right direction. The two things that make it succeed or fail are (a) treating **Grok output as a proposal that goes through the same validator every human-made style goes through**, never straight into a build (no human approval queue — the validator and the preview are the gate), and (b) building the **project + sync layer before** the AI layer, because memory, enrichment and "compare with last month" all need a stable per-project store to hang off. Detail below, including the trade-off on removing the X login, which is real and worth deciding with eyes open.

---

## 1. Projects — the new spine

### 1.1 Model

```
Project
  id, slug, name, client, colour/emoji, created_by, created_at
  sources[]         Sheet | Upload | Paste | Links (see §2)
  styles[]          slugs from the style pool; ≥1; each has {enabled, outputs[]}
  defaults          platform mix, dedupe, keep_engagement, workers, report name pattern
  memory            Grok memory (see §4.3): style facts, glossary, thread history
  runs[]            = today's jobs, but grouped and comparable
  schedule          none | on-sheet-change | daily/weekly cron
  share             optional read-only link for the interactive dashboard
```

Storage: **SQLite stays** (already there, zero ops). New tables `projects`, `sources`, `project_styles`, `runs` (rename of `jobs` with `project_id`), `memory`, `enrichments`. Files live under `data/projects/<slug>/` (sources cache, screenshots cache, outputs). Old jobs migrate into an "Unsorted" project so nothing is lost.

### 1.2 Navigation (left bar only — no tabs, no top-bar picker)

```
PROJECT  [ 🪔 Kashi Ke Wasi — Monthly ▾ ]   ← dropdown: list of projects, "+ New project" at the bottom
  Overview      last outputs, next-run status; metrics & charts behind an optional toggle (off by default)
  Sources       Google Sheet (live), Excel uploads, paste, links; sync log; "Add source ▾" menu
  Styles        the project's picks from the pool; run options as checkboxes; Generate
  Runs          history for this project; compare; Enrich
  ✦ Grok Studio chat for THIS project (samples, proposals, memory, sessions are per project)
  API           this project's key + 3-step usage
  Settings      name, client, schedule, share link, budget
LIBRARY
  Style pool    global gallery + designer
ADMIN
  Users & roles · Health & capture
```

Switching the project in the dropdown swaps every page above it — the pages are the same for all projects, only the data changes. There is no separate "projects grid" page; the dropdown *is* the project list.

Keyboard: `O` overview, `N` new run in current project, `G` Grok studio, `S` style pool.

UI rule: fewer pills, more controls — selects for anything with >3 choices, checkbox groups for on/off options, one "Add ▾" button with a menu instead of a row of buttons.

### 1.3 Multi-style runs

A project can select 1–N styles. A run then produces one bundle per style (`Twitter Report (letter).pdf`, `Combined deck.pptx`, …) **from a single capture** — screenshots are taken once and laid into every selected style. This is where the profile engine pays off: capture and layout are already separate. The run page shows one progress bar for capture and one row per style for build.

---

## 2. Data sources and sync

| Source | How | Sync |
|---|---|---|
| **Google Sheet (live)** | Published-CSV URL as today (kept: strict host allow-list, size cap, content-type check). New: pick the **tab** via `gid`, remember column mapping (Section / Handle / Link / metric columns) once per project. | **Yes** — see below |
| **Excel / CSV upload** | As today; kept per project as a version list ("links_v3.xlsx, uploaded 12 Aug") | Re-upload replaces; old versions kept 30 days |
| **Paste** | As today; saved as a source snapshot | manual |
| **Links** | Single links added from the workspace or via a bookmarklet | manual |
| **Later:** private Google Sheets via OAuth, Notion DB, Airtable | provider adapters, same `Source` interface | — |

**Sync design ("always in sync" without hammering Google or the server):**

* A background *sync loop* per project with a Sheet source. Interval is adaptive: 5 min while the project is open in someone's browser or ran in the last 24 h, 1 h otherwise, paused when nobody has opened the project for 14 days. Manual "Sync now" always available.
* Fetch with `If-None-Match`/`If-Modified-Since`; then hash the CSV body. **No change → nothing happens** (no parse, no preview, no job).
* Change → re-run the *preview* only (rows / duplicates / dropped-with-row-numbers) and store a **diff** against the last snapshot: `+7 links, −2 links, 3 metric cells changed`. The project card shows "Sheet changed · 7 new links" and, if `schedule = on-sheet-change`, queues a run.
* Every run records the source snapshot hash it was built from — so "regenerate the July report" is reproducible even after the sheet moved on.

---

## 3. Style pool

The existing style gallery + designer becomes the **pool**: built-ins, shipped profiles, designer-made template styles, and Grok-generated ones. Additions:

* **Filters** on the pool as selects + checkboxes (platform, page size, numeric/template, AI-drafted, mine, sort).
* **Style card → Add to project** and **project → Pick from pool** (multi-select).
* Every style still passes `registry.validate` before it can be picked. **This is unchanged and is the guardrail for §4.** There is no pending/approved queue in v3 — a saved style is usable immediately (curation stays possible later via `style_settings.json` hide/show).
* New output kind: **`web`** — an interactive HTML report (single self-contained file, same slots + a chart page + clickable links + light/dark). This is the "interactive visuals" deliverable, alongside the interactive project dashboard. Template styles produce `pdf, pptx, web`; numeric ones `pdf, docx, web`.

---

## 4. Grok Studio (xAI API)

### 4.1 What Grok is used for — and what it is not

| Grok does | Grok never does |
|---|---|
| Reads 2–10 sample reports and proposes a **profile JSON** in our existing schema (page size, grid, slots, fonts, colours, cover/summary/end pattern) | Touch a number that came from the sheet — metrics print from the sheet, always |
| Talks (text/voice) about the look: "make the metric pills smaller, cover in Hindi, add the client logo top-left" → emits a *diff* to the profile | Bypass `registry.validate` |
| Writes enrichment text: section summaries, headline insight, post tags, run-over-run comparison | Screenshot anything (capture stays deterministic Playwright) |
| Suggests page art *descriptions* (optionally generates art via image model if we enable it) | Decide silently — every change is shown as a proposal card with Accept / Edit / Discard |

Provider adapter `ai/provider.py` with `GrokProvider` (chat, vision, tools) so a second provider can be dropped in; **`XAI_API_KEY`, `XAI_MODEL`** in `.env`. All Grok calls run in the existing job queue (they are slow, not on the request thread), results cached by content hash.

### 4.2 Template learning — the flow

1. **Drop samples**: 2–10 files (PDF / PPTX / DOCX / PNG / JPG). Server rasterises pages at ~110 dpi (PyMuPDF for PDF; LibreOffice headless for PPTX/DOCX only if present, otherwise ask for a PDF export). Cap: 40 pages total sent to vision, downsampled to ≤1280 px long edge — cost control.
2. **Grok vision pass** with a strict system prompt and a JSON schema (tool call) that mirrors `registry._ALLOWED`. It returns: page size & orientation, page types found (cover / summary / post / end), per page-type slots as % boxes with roles (screenshot, title, account, date, metric:like, link, logo, section…), typography (family guess, weights, sizes as % of page height), colours (hex), and a **confidence + notes** per field. Plus a *style brief* in plain language ("dark navy header band, rounded screenshot with soft shadow, metrics as pills bottom-left").
3. **Server builds a candidate profile** from that JSON, runs `registry.validate` and `tpl_preview.page_png` (existing) to render one real preview page. Failures come back to Grok once with the validator's message ("slot outside page", "unknown key") — one repair round, then stop.
4. **Studio shows** the proposal *inline in the chat* as a card: sample page ⇄ generated preview side by side, confidence chips, brief, and buttons **Save to pool + use in project · Open in designer · Show slot list · Undo**. User adjusts by chat/voice or in the existing designer (it *is* a normal template profile). Save puts it straight in the pool tagged "AI-drafted from 6 samples" and adds it to the project.
5. Page art: v3.0 uses a **flat generated background** (colours/bands/rounded frames drawn by PIL from the brief). Fancy Canva-grade art stays a human job — the guide PNG flow already exists. Optional v3.1: image model for art.

### 4.3 Memory

Per project (and optionally per user): a `memory` table with three parts.

* **Style facts** — small JSON the model reads on every call: preferred page size, colour tokens, "no engagement counts on Twitter report", "client name always in Devanagari on cover", metric labels the client uses.
* **Glossary / voice** — how this client writes: tone, words to use/avoid, sections they care about.
* **Thread** — the studio chat (text and transcribed voice), summarised by Grok every ~30 turns into the two structures above so context stays small. Raw thread kept for audit; only the summary + facts are sent on later calls.

Memory is **visible and editable** in the studio (a plain list with a pencil), so nothing the model "remembers" is a black box.

### 4.4 Voice

Browser **Web Speech API** for speech-to-text (free, no server load, works in Chrome/Edge; Safari via webkit prefix). Push-to-talk mic button in the studio; transcript appears as text before sending, so it is correctable. Replies read back with `speechSynthesis` if the user toggles it. If xAI exposes realtime voice via API later, the adapter is where it plugs in. **Not** shipping server-side Whisper in v3 — it is exactly the kind of load this release is cutting.

### 4.6 Studio layout (Claude-style)

Grok Studio is a **project page** — the chat, samples, proposals, memory and sessions belong to the selected project; switching the project in the left bar switches the whole studio context. Layout: **centre = chat** — a session header (which project, which mode: *Template design · Enrich a run · Ask about data*), the thread with proposal cards inline, and a composer with attach (samples), push-to-talk mic, mode select and a token/cost meter. **Right rail = context**, collapsible sections: *Samples* (thumbnails, page budget), *Proposals* (click to jump/restore), *Memory* (editable facts), *Project context* (auto: sources, styles, last run, sections), *Sessions* (past threads). Nothing else on the page. Voice: browser speech-to-text; optional read-aloud toggle in the header.

### 4.5 Enrich (one click on a finished run)

Runs as a job step after the build, on the run's `results.json` + sheet metrics + project memory. Produces an **enrichment layer** stored beside the run (never rewriting captured data):

* Headline insight (2–3 lines) and one summary paragraph per section.
* Per-post tags (topic, sentiment, format: text/photo/video/reel), top-N by each metric, notable outliers.
* **Run-over-run**: vs the previous run in the same project (posts +12, avg impressions −8%, 3 handles new).
* **Charts** (rendered as PNG by matplotlib for PDF/DOCX/PPTX; live SVG in `web`): posts by section, platform mix, metric distribution, timeline.
* Optional **public-metric fill**: for X links, fetch public counts from the syndication JSON (see §5) into blank sheet cells, flagged *"public data, {date}"* — never overwriting a value the sheet already has.

Delivery: an "Enriched" toggle on outputs — regenerate the documents with an *Insights* page (or a slide) inserted after the cover, and the interactive `web` report gets the charts inline. The plain version stays downloadable. Cost & tokens shown on the run page.

---

## 5. Capture without an X account

Decision: remove `X_USERNAME/X_PASSWORD/X_EMAIL/X_TOTP_SECRET`, `sessions/x_state.json`, `webapp/x_login.py`, the *X login status* page, `save_login.py` and the sign-in warm-up.

**Replacement engine `x/embed_capture.py`** — captures through X's public **embed renderer** (`platform.twitter.com/embed/Tweet.html?id=<id>&theme=light&hideCard=false&hideThread=false&lang=en`, plus `cdn.syndication.twimg.com/tweet-result` for JSON). Why this fits the Twitter report specifically:

* Renders **logged-out**, no sign-up sheet, no scroll lock, no feed — a tiny page, so a browser context costs a fraction of the full X app. Faster and lighter per shot.
* A reply embed shows the **parent above the reply** (`hideThread=false`) — the "reply shot with its parent" promise the Twitter report is defined by.
* Deterministic DOM: crop points ("above the time·views line" vs "below the action bar") are stable selectors, so *keep engagement* stays a tick.
* Public metrics (likes, reposts, replies, views, followers) come from the syndication JSON — feeds Enrich and the Influencer metrics without scraping the app.

**What is lost, plainly:** age-gated / sensitive-media posts do not render in the embed (they did not reliably render logged-in either — the README already lists them as skipped); protected accounts never render; X can rate-limit an IP that captures hundreds of embeds fast (mitigation: cache + gentle concurrency; far softer than app rate-limits). Keep the existing logged-in `x_capture.py` **in the repo but off** behind `X_LEGACY_LOGIN=1` for one release as an escape hatch; delete in v3.2 if never needed. Facebook and Instagram engines are already logged-out — unchanged.

---

## 6. Server-load diet — what goes, what replaces it

| Today | Load it causes | v3 |
|---|---|---|
| Per-job **copy of the code tree** into `data/jobs/<id>/app` | disk churn, I/O per job; existed only because the frozen pipeline anchors paths to `__file__` | Runs read the installed package; a job dir holds **inputs and outputs only** |
| **One Chromium process per worker**, per job | 0.5–1 GB each, cold start every job | **One long-lived browser per host**, N *contexts* from a pool; embed pages are light so 6–8 contexts fit where 3 processes did |
| Screenshot every link every run | same links re-shot for monthly reports | **Screenshot cache** keyed by `(url, engine, keep_engagement, dpr)`; TTL 7 days by default; project setting "always fresh" for those who need it |
| Build **every** format then filter | CPU for files nobody downloads | Build only ticked outputs; DOCX/PPTX built **on first download** if not ticked at run time |
| Server-rendered designer **preview per change** (PIL, 800 ms debounce) | CPU on the request path | Preview drawn **in the browser** (Canvas, from the same fractions); the server render remains only for the *final* "Preview page" button and the Canva guide |
| X **login warm-up** at start + session polling + health probes on a timer | background browser use for nothing | Health checks **on demand** (open Admin → probe once, cache 10 min) |
| **Session-owned downloads** with per-request re-checks | fine, keep — but | file responses via `X-Accel`/sendfile behind nginx, not streamed by Python |
| Preset table | superseded | Folded into projects (a preset = a project with one style) |
| `MAX_CONCURRENT_JOBS=1` | queue backlog while CPU idles | Capture concurrency measured in **contexts**, not jobs; queue interleaves projects |
| Thumbnails at startup | fine, keep | unchanged (hash-in-filename) |
| Google Sheet fetched on every preview | repeated fetch | ETag + content hash; sync loop owns the fetch |
| Retention sweep | keep | plus a **budget** per project (MB) with oldest-run pruning |

Expected effect on the 4 GB VPS: from ~3 parallel shots to ~6–8, cold job start from ~15 s to ~2 s, repeat runs mostly cache hits, idle CPU near zero (no warm-ups, no timers).

---

## 7. Interactive visuals

Two surfaces, one chart component library (plain SVG, no build step, matches the app's CSS tokens, light/dark selected separately):

* **Project Overview**: by default only *Last outputs* and *Next run*. A **"Show metrics & charts" toggle** (off by default, remembered per user) reveals KPI tiles (posts, handles, skipped), *posts by section*, *platform mix*, *runs over time*. **Post-level metrics** (likes/impressions per post, top posts) are **deferred** — they need the backend metric pipeline (sheet columns + syndication JSON) and are not in 3.0.
* **`web` output**: the report itself as a single HTML file — cover, summary, one card per post (screenshot, metrics, LINK), plus an Insights page when enriched. Openable offline, shareable by the project's read-only link.

Charts follow the dataviz rules already used in the demo: one axis, fixed categorical order (blue → orange → aqua), 2 px surface gaps, hover tooltips, table view toggle.

---

## 8. Phasing (each phase shippable on its own)

| Phase | Scope | Notes |
|---|---|---|
| **3.0-a — Engine** (1–2 wk) | `x/embed_capture.py` + syndication JSON reader; browser pool with contexts; screenshot cache; remove X login & warm-up; job dir without code copy | Acceptance: the same 20-link mixed list (`acceptance_parent_fix.py mixed`) — zero silent parent losses through the embed path; RULEBOOK "open the PDF and look" |
| **3.0-b — Projects & sources** (2 wk) | tables + migration into "Unsorted"; project cards; workspace shell; Sources tab with sheet sync loop, diff, snapshot hash; Runs tab; multi-style run from one capture | Preview/submit APIs gain `project_id`; presets migrate |
| **3.0-c — Style pool + web output + Overview** (1–2 wk) | pool tags/filters, add-to-project, `web` output kind, chart lib, project Overview dashboard, client-side designer preview | `registry.OUTPUTS += web`, `prof_builder.BUILDERS['web']`, `tpl_builder` web renderer |
| **3.1 — Grok Studio** (2–3 wk) | provider adapter, sample ingestion + rasterise, vision → profile JSON, validate/repair loop, chat-centred studio with context rail, proposal cards, chat + voice, memory (facts/glossary/thread + summariser) | Grok never writes to disk without validate; token/cost meter |
| **3.1-b — Public API** (1 wk) | per-project keys, scopes, limits, `/v1/runs` + outputs + webhook, test keys, usage | see §11 |
| **3.2 — Enrich** (1–2 wk) | insights, tags, run-over-run, chart images into PDF/DOCX/PPTX, Insights page, public-metric fill (flagged) | "Enriched" toggle; plain build always kept |
| **3.3 — Ops** | budget per project, sendfile downloads, on-demand health, delete legacy X login | — |

Rulebook carry-overs: keep the frozen `run.py`/`influencer/` runnable during 3.0-a as parity oracles; every new dependency to `requirements-web.txt` (PyMuPDF for PDF rasterising; xai SDK or plain `httpx`).

---

## 9. Config additions (`.env`)

```
XAI_API_KEY=            # Grok
XAI_MODEL=grok-4        # chat/vision; overridable
AI_MAX_SAMPLE_PAGES=40  # cost cap for template learning
CAPTURE_CONTEXTS=6      # browser contexts in the pool (replaces WORKERS/MAX_WORKERS)
SHOT_CACHE_DAYS=7
SHEET_SYNC_ACTIVE_MIN=5 # while a project is open / recently run
SHEET_SYNC_IDLE_MIN=60
X_LEGACY_LOGIN=0        # escape hatch, removed in 3.2
```

Removed: `X_USERNAME X_PASSWORD X_EMAIL X_TOTP_SECRET WORKERS MAX_WORKERS INFLUENCER_WORKERS`.

---

## 11. Public API — one key per project, one call

The key *is* the project: a caller sends links and gets back exactly that project's report (its styles, sources, memory, enrichment). No style/platform/options in the request — those live in the project. That keeps integrations tiny, keeps clients' data separate, and makes revocation one click.

Kept deliberately simple:

* **One live key per project** (`vr_<slug>_<random>`, shown once, stored hashed) + **one test key** that returns a sample report without capturing. *Regenerate* replaces the key.
* **Two endpoints:** `POST /v1/run` with `{ "links": [...] }` (optional `name`, per-link `section`, `webhook`) → `{run_id, status}`; `GET /v1/run/{id}` → `{status, pdf, pptx|docx, web}` (signed URLs, 7-day expiry). Webhook called on completion if given.
* **Fixed limits** per project (200 links per call, 60 calls/hour) shown on the page; adjustable in Settings later, not exposed as knobs to the caller.
* Scopes, per-key limits, an org-level management key: **later, only if a client asks.**

## 10. Open questions for you

1. Read-only **share link** for the project dashboard / web report — public-by-secret-URL, or only for signed-in colleagues?
2. Should Enrich be allowed to **write into the Google Sheet** (public-metric fill) or only into the report? Plan above says report only.
3. Which of the two built-in reports (Twitter letter, Influencer A4) is the first to be re-expressed as a profile so the frozen pipelines can retire? I'd start with Twitter, since the embed engine changes its capture path anyway.

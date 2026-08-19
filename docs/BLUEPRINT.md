# VedicReport — Blueprint (v3.0-a, August 2026)

One file that describes the whole system precisely enough to rebuild it — or
to hand to a new session and say "redesign this without breaking it". Read
`RULEBOOK.md` alongside; this file says *what is where and how it fits*, the
rulebook says *what will bite you*.

---

## 0. In one paragraph

Give it post links (X/Twitter or Facebook); it screenshots every post in a real
browser and lays the screenshots into documents — PDF + DOCX for the numeric
styles, PDF + an editable PPTX deck for the designed-page (template) ones.
Two capture pipelines are **frozen** and production-proven (`run.py`+`src/`
for the Twitter report, `influencer/` for the Influencer report). Everything
else — a third engine for Facebook, a *profile engine* that turns page layout
into JSON, and a FastAPI web app with a role-aware dashboard — is built
**around** them, never inside them.

---

## 1. Layers (top → bottom)

```
┌───────────────────────────────────────────────────────────────────────┐
│ webapp/ (FastAPI + Jinja + one CSS + one JS)                           │
│  pages: / (New report) /history /styles /jobs/<id> /admin/* /settings │
│  api:   /api/preview /api/jobs /api/presets /api/styles               │
│  roles: APP_ADMINS → admin | colleague                                │
├───────────────────────────────────────────────────────────────────────┤
│ jobs/  store.py (SQLite)  queue.py (thread pool)  runner.py (subprocess│
│        per job in data/jobs/<id>/app — a private COPY of the code)    │
├───────────────────────────────────────────────────────────────────────┤
│ entrypoints (one per report type; the runner picks by capability table)│
│   run.py                      → Twitter report          [FROZEN]      │
│   influencer/run_influencer.py→ Influencer report       [FROZEN]      │
│   profiles/run_profile.py --profile <slug> → every other style        │
├───────────────────────────────────────────────────────────────────────┤
│ engines (one browser page in, one result dict out)                    │
│   src/capture/x_capture.py   X, engagement cropped/kept  [FROZEN]     │
│   influencer/inf_capture.py  X, engagement kept + metrics [FROZEN]    │
│   facebook/fb_capture.py     Facebook public post, logged-out         │
│   instagram/ig_capture.py    Instagram public post, logged-out        │
│   engine "combined"          per-link choice of the three (prof_worker)│
├───────────────────────────────────────────────────────────────────────┤
│ builders (results.json + PNGs → documents)                            │
│   src/report_builder.py [FROZEN]  influencer/inf_report_builder.py [FROZEN]│
│   profiles/prof_builder.py  (PDF/DOCX from a profile JSON)            │
└───────────────────────────────────────────────────────────────────────┘
```

**Frozen** = byte-identical to the tested state, edited only through the
approved edits listed in RULEBOOK rule 1. Verify with
`git diff <first-commit> -- run.py src/ influencer/ install.py requirements.txt`.

---

## 2. Directory map (what each file is for)

```
run.py                     Twitter report CLI                              FROZEN
save_login.py              one-time manual X login → sessions/x_state.json  FROZEN
install.py                 deps + browser                                  FROZEN
requirements.txt           CLI deps                                        FROZEN
requirements-web.txt       web deps (any NEW dependency goes here)

src/                       the X pipeline                                  FROZEN
  run_report.py            workers, retry pass, quality pass, results.json
  _worker.py               one Chromium per process
  capture/x_capture.py     the X capture (reply + parent framing)
  overlays.py              take X dialogs off the page, release scroll lock
  shot_quality.py          blank/black/undersized detection
  report_builder.py        Twitter PDF + DOCX
  input_loader.py          reads .xlsx / lists; X-only filter at the end
  platforms.py, save_sessions.py

influencer/                the Influencer pipeline (parallel to src/)      FROZEN
  run_influencer.py, inf_runner.py, inf_worker.py, inf_capture.py, inf_report_builder.py

facebook/                  the Facebook engine (rule 18 folder)
  fb_capture.py            capture(page, url, shot, keep_engagement) → result dict
  README.md
instagram/                 the Instagram engine (rule 18 folder), logged-out
  ig_capture.py

profiles/                  the profile engine — presentation as data
  registry.py              load/merge/validate profile JSON; ENGINES; user dir
  registry/*.json          shipped profiles: twitter, influencer (parity oracles),
                           client-deck, contact-sheet, twitter-hi-res, facebook
  netlinks.py              which links belong to which platform ('combined' = any);
                           platform-neutral row reader (+ Section, + sheet metric
                           columns Like/Impressions/Views/Reach/…). Reads the
                           SECTIONED sheet the team keeps by hand — no link, handle
                           or section column; section names in column A; the header
                           row's own first cell is the first section; a "Reach/views"
                           column feeds both metrics; blank-A number rows are junk.
                           Recognised by shape (metric_header), never by a mode.
                           A row the sheet did not name is flagged `account_auto`,
                           and prof_worker puts the CAPTURED name in its place.
  assets/logos/            platform marks drawn by PIL, used by template logo slots
  registry/combined-16x9.json + assets/combined-16x9/   the shipped Combined preset
  layout.py                page geometry → placements
  shapes.py                pure-PIL image ops (fit/pad/round/border/shadow)
  progress.py              the stdout vocabulary the web runner parses
  prof_runner.py           workers/retry/quality/results.json (own CTX_KWARGS)
  prof_worker.py           imports the engine DIRECTLY (x | influencer | facebook)
  prof_builder.py          PDF/DOCX from profile + results.json; --outputs
                           narrows that to what the job asked for
  tpl_builder.py           the same for TEMPLATE styles (designed PNG pages + slots);
                           swaps in a Unicode face per string when Helvetica
                           cannot draw it (captured Hindi Page names — rule 14)
  tpl_preview.py           the design kit: Canva slot guide + one-page preview
                           (PIL; same fractions as tpl_builder — RULEBOOK §18a)
  thumbnails.py            renders the style thumbnails from real geometry
  run_profile.py           entrypoint mirroring run.py's CLI
  tests/run_all.py         8 zero-capture suites (parity, dispatch, inputs,
                           sectioned sheet, …)

webapp/
  main.py                  app, pages, auth routes, _shell() context, asset_v
  config.py                .env → settings; APP_ADMINS; public_settings()
  auth.py                  sessions, CSRF, rate limit, require_user/admin
  report_types.py          PLATFORMS + ReportType capability table (built-ins + profiles)
  health.py                per-platform session health; capture budget
  uploads.py               parse/validate uploads; analyse(grid, dedupe, platform)
  sheets.py                Google Sheets published-CSV fetch (SSRF-guarded)
  previews.py              thumbnail manifest for cards
  styles.py                style designers (numeric + template), assets under
                           data/profiles/assets/<slug>/, curation (style_settings.json)
  routes_jobs.py           /api/preview /api/jobs… /download
  routes_extras.py         /api/presets /api/styles (+ /guide, /preview-page)
  x_login.py               headless X sign-in for the shared account
  jobs/store.py            SQLite: jobs, presets, login_attempts
  jobs/queue.py            bounded worker pool
  jobs/runner.py           job dir, command, progress regexes, publish artifacts
  jobs/cleanup.py          retention
  templates/               base (shell), index, history, job, styles, users,
                           session_status, settings, login, error, _brand.svg
  static/app.css app.js    the entire front-end; no build step

scripts/probe_logged_out.py   "can this URL be captured without an account?"
scripts/make_samples.py       sample PDFs per profile
docs/                          this file, profile-engine.md, roadmap.md, dashboard-mockup.html
data/  (runtime, gitignored)   jobs/<id>/, jobs.db, profiles/ (user styles), samples/
sessions/ (secret, gitignored) x_state.json, optional fb_state.json
```

---

## 3. The one job lifecycle

1. **Preview** `POST /api/preview` (file | text | sheet_url, platform, dedupe,
   link_col, account_col) → rows / duplicates / dropped-with-row-numbers.
   No job, no browser.
2. **Submit** `POST /api/jobs` — same parsing path (`_grid_from_request` +
   `uploads.analyse`) so preview == capture. `report_types.check_runnable(
   platform, type)` is the gate. Creates `data/jobs/<id>/`, copies
   `run.py src/ influencer/ profiles/ facebook/` in (plus the chosen user
   style JSON), symlinks `sessions/`, writes canonical `input.xlsx`.
3. **Run** `runner.run_job` → `build_command()` picks the entrypoint from the
   capability table (never from a slug test), starts the subprocess in the job
   dir, parses its stdout with the `_RE_*` regexes (the *contract* in
   `profiles/progress.py`), counts PNGs for the live bar, watchdog timeout.
4. **Publish** newest `reports/*.{pdf,docx,pptx,xlsx}` + `screenshots.zip` →
   `out/`, filtered to the formats the job asked for (`publish(..., wanted)` —
   for the two built-ins that filter IS the choice, since their entrypoints are
   frozen); skipped links get a plain-English reason from `_STATUS_REASON`.
5. **Deliver** `/jobs/<id>` polls `GET /api/jobs/<id>` (or NDJSON stream in
   inline mode); downloads are owner-checked and path-checked.

---

## 4. Contracts you must keep

| Contract | Where | Breaks what if changed |
|---|---|---|
| Result dict `{status, url, handle, screenshot, text, overlay, frame_ok, parent_lost[, metrics]}` | every engine | prof_runner gates, builders, skipped list |
| stdout lines `[runner] N X link(s) loaded`, `[verify] a/b …`, `[report] wrote …`, … | `profiles/progress.py` ↔ `webapp/jobs/runner.py` | the progress bar goes blank silently (test_progress_contract asserts it) |
| `reports/results.json` + `reports/screenshots/*.png` in the job's app dir | all runners | publish() finds nothing |
| Profile schema v1 (`registry._ALLOWED`, `_TOP`); unknown key = error | registry.py | designer + presets store slugs |
| Outputs are per style: template → `pdf, pptx`; numeric + built-ins → `pdf, docx` (`registry.TEMPLATE_OUTPUTS` / `NUMERIC_OUTPUTS`, `ReportType.outputs`) | registry.validate, report_types | the form offers a format the job cannot produce |
| `ReportType(slug,label,argv,worker_pool,allows_worker_choice,allows_keep_engagement,platform,custom,…)` | report_types.py | form controls, build_command |
| Platform ↔ engine: `ENGINES[engine]["platform"] == profile.platform` | registry.validate | wrong link filter for a style |
| Canonical upload = `Account | Link` sheet | uploads.write_canonical_xlsx | frozen loader reads exactly those headers |
| Static assets linked as `?v=<sha1 of css+js>` | main.py `asset_v` | browsers pair old CSS with new HTML |

---

## 4a. Projects (v3.0-a) — the spine every page hangs off

A **project** is a client or a recurring report. It owns the styles that print
it (picked from the pool, one or more, each with its own file formats) and every
job belongs to exactly one project. Projects are shared by the team.

| Piece | Where |
|---|---|
| tables `projects`, `project_styles`; `jobs.project_id` | `webapp/jobs/store.py` (`_ADDED_COLUMNS`, migration to the **Unsorted** project on first boot) |
| current project = `request.session["project_id"]` | `webapp/projects.py` (`current`, `select`, `styles_of`, `public`) |
| API `/api/projects` (list · create · select · patch · delete/archive · PUT styles · POST styles/{slug}/background) | `webapp/routes_projects.py` |
| pages `/` Overview · `/new` New run · `/runs` · `/project/styles` · `/project/settings` (`/history` → `/runs`) | `webapp/main.py`, `templates/overview.html`, `index.html`, `history.html`, `project_styles.html`, `project_settings.html` |
| left bar: PROJECT dropdown (list + "+ New project") then Overview / New run / Runs / Styles / Settings; Sources and API are greyed "soon" | `templates/base.html`, `app.js` shell block, `app.css` `.pdrop*` |
| New run ticks **one or more** of the project's styles → `POST /api/jobs` with repeated `report_type` → one job per style (same links; a shared capture is the 3.0-b optimisation) | `routes_jobs.submit_job`, `app.js initSubmitForm` |
| Presets are no longer offered (a project replaces them); the API and table remain | `routes_extras.py` |

**Page background + PPTX for numeric styles.** `page.background` = `null` |
`{"color": "#RRGGBB"}` | `{"image": "background.png"}` (`registry._validate_background`,
`background_path`, `background_color`). Painted first on every PDF page and
every PPTX slide by `prof_builder` (`_background`, `_palette` — light ink on a
dark colour); the image lives in `assets/<slug>/background.png` so the runner's
one copytree carries it. `NUMERIC_OUTPUTS` = pdf · docx · pptx;
`prof_builder.build_pptx` = one slide per page, screenshots as pictures,
captions as text boxes (post link = real hyperlink), cover and Links slides.
Set from the project Styles page (`styles.set_background`); a shipped style is
first copied into a project-owned one (`styles.fork_for_project` →
`"<src>-<project>"`) and swapped in the project (`store.project_replace_style`).
Test: `profiles/tests/test_projects.py`.

## 4b. Sources + the smart sheet reader (v3.0-a, second half)

| Piece | Where |
|---|---|
| **Smart reader** — any layout: every URL in any cell is a post (multi-line cells too), headings above links are sections, dates anywhere ("17/8/26", "Date- 4-7-26", "14 Jul") set the date, numbers beside links are metrics named by the header, short text beside a link is the account. Output = canonical `Section | Handle | Link | <metrics>` grid → `uploads.analyse` unchanged | `webapp/smartsheet.py` (`understand`, `latest_block`, `list_tabs` from the workbook's public htmlview page, `read(url, mode=latest|tab|all)`, `fingerprint`, optional `grok_label_columns` when `XAI_API_KEY` is set) |
| New run → Google Sheet uses it: `sheet_mode` (newest date / one tab / all), tab picker by name, `sheet_info` in the preview | `routes_jobs._grid_from_request`, `index.html`, `app.js` |
| **Sources** — a project's watched sheets: `sources` table; `check_source` reads → fingerprint → on a NEW DATE (or any change, per `trigger`) starts a run with the project's styles; background loop every `SHEET_SYNC_MINUTES`; first read is a baseline, never a run | `webapp/sources.py`, `webapp/routes_sources.py` (`/api/projects/{pid}/sources`: inspect · create · patch · delete · check · run), `templates/project_sources.html`, `app.js initProjectSources` |
| **One place jobs are created** — New run and the sync loop both call it | `webapp/runs.py create_run` |
| **No link limit** — `MAX_LINKS=0` (default) means unlimited; a positive number still caps | `config.py`, `uploads.analyse`, `routes_jobs` |
| Fetch guards unchanged; `sheets.fetch_text(allow_html=True)` added for the htmlview page | `webapp/sheets.py` |
| Tests | `profiles/tests/test_smartsheet.py` (the real workbook's four shapes) |

## 4c. Report look (v3.0-b) — pills, cover fit, clean template, background swap, FB reels

| Piece | Where |
|---|---|
| Text slot `label` + `pill` (#RRGGBB): "Like 676" drawn in a rounded pill by the APP — only when the value exists, so a metric the sheet lacks leaves nothing behind. Same rule in PDF, PPTX (a real rounded shape carrying the text) and the PIL preview | `registry._TEXT_KEYS`, `tpl_builder._text_value/_pill_box/_pill_ink`, `tpl_preview._draw_text` |
| `template.fit` = fit / cover; cover = full width, crop the BOTTOM of a tall post, pad below a short one — never crops sideways (`shapes.cover_top`, `image.fit="cover"`, aspect from the first slot) | `registry.FITS`, `shapes.compose`, `styles._template_profile` |
| **Combined clean (16:9)** — PIL-drawn page art with NO baked text; logo · handle · date · Post i · metric pills · "Open post" button · cover-fit screenshot; cover + summary | `profiles/registry/combined-clean-16x9.json` + `assets/combined-clean-16x9/` |
| Designer: Screenshot fit select, per-text Label + Pill; "Place standard slots" now lays out the clean design | `templates/styles.html`, `app.js initTemplateDesigner` |
| **Replace background** of a designed-page style per project (page art swap, slots stay) — forks a shipped style into the project first | `styles.replace_page_art`, `POST /api/projects/{pid}/styles/{slug}/pages`, `project_styles.html` |
| Facebook **reels**: `/reel/<id>` retried on `/watch/?v=` then the public video plugin before "unavailable" | `facebook/fb_capture.py` (`_reel_alternatives`, `_capture_plugin`) |
| Sheet-reader notes ("unnamed number column…") also land in each job's activity log | `runs.create_run(notes=…)` |

## 5. Front-end model (for a redesign)

Shell: top bar (brand · theme · avatar menu) · left nav (New report N,
History H, Report styles S; **Admin**: Accounts & sessions, Settings, budget
meter — only when `is_admin`) · main. No right rail. Everything user-facing is
server-rendered Jinja + `app.js` page inits (`initSubmitForm`, `initJobPage`,
`initHistory`, `initStyles`); state lives in the DOM and in the API, never in
a front-end store. Tokens live at the top of `app.css` (light + dark).

New report = Platform pills → compact style rows (Sample opens a modal with
the readable page) → Links (file / paste / sheet tabs + preview table) →
capability-driven options (Outputs tick boxes, crop, capture speed) → sticky
action bar (name · summary · Save preset ·
Generate). Design reference: `docs/dashboard-mockup.html`.

Roles (`auth.role_of`): **admin** (everything, incl. Users & roles and style
curation), **designer** (template + edit their styles; stay *pending* until an
admin approves), **member** (reports). Users live in SQLite (`users` table,
managed at /admin/users); `.env` APP_USERS/APP_ADMINS remain the bootstrap
fallback. `auth.require_admin` / `require_designer` guard pages AND write APIs.

Styles on New report = `styles.visible_types()`: built-ins always; shipped
profiles unless hidden; app-designed styles only once approved
(`data/style_settings.json`).

**Template styles** (Canva flow): a designer exports page PNGs (post, optional
cover/summary/end), uploads them at /styles, drags screenshot slots and text slots
(title/date/page/account/link/category/metrics) on the page. Stored as a
normal profile with a `template` section (`registry._validate_template`);
assets in `data/profiles/assets/<slug>/` (page PNGs + `fonts/`), copied into the
job dir with the profile. `layout.placements` fits screenshots into slots (never
crops); `tpl_builder` paints background + slots. **Outputs: PDF + PPTX**, both
exact — the PPTX is one slide per page with the art as a background picture and
every slot a native, editable object (picture / text box with a real hyperlink /
a real table for the summary). DOCX is not offered for these: Word cannot layer
a picture over a full-page background, and PPTX replaces it as the editable
format. There is no trailing links page (every post carries its own LINK).

**The design kit (v2.3)** turns that from "place boxes blind" into a loop:

| Piece | Where | What it is |
|---|---|---|
| Make my own version | `[data-mine]` → `loadTemplate(slug, true)` | any template style's slots, its art greyed as a placeholder, `copy_from` in the meta so unreplaced pages are copied on save |
| Canva slot guide | `GET/POST /api/styles/guide` → `tpl_preview.guide_png` | transparent PNG at the page's pixel size (16:9 → 1920×1080), every slot a labelled outline |
| Live preview | `POST /api/styles/preview-page` → `tpl_preview.page_png` | ONE page with sample data + a fixture screenshot, auto-refreshed 800 ms after a change |
| Editor ergonomics | `app.js initTemplateDesigner` | arrow-nudge, ⌘D duplicate, snapping with guide lines, numeric X/Y/W/H %, text presets, live slot numbering |
| Template fonts | `template.fonts` + `text.font` | ≤3 × ≤2 MB .ttf/.otf per style in `assets/<slug>/fonts/`; `pdfmetrics.registerFont` embeds the file in the PDF, the PPTX can only NAME the family (read from the file, announced on stdout) |

All of it goes through `styles._template_profile` → `registry.resolve`, so the
guide, the preview and the save agree by construction. `tpl_preview` is a
deliberate second implementation of the drawing rules in PIL (no rasteriser in
this project) — see RULEBOOK §18a for what that costs and what pins it down.

---

## 6. Adding things (recipes)

- **New style (layout only)** → Report styles → designer, or a JSON in
  `profiles/registry/`. No code.
- **New platform / capture behaviour** → new folder `<net>/<net>_capture.py`
  returning the result dict; add engine to `registry.ENGINES` and platform to
  `report_types.PLATFORMS` (live=True) + `netlinks.MATCHERS`; a
  `<net>.json` profile; a `_<net>_health()`; add the folder to
  `runner._CODE_ITEMS` and the Dockerfile. `prof_worker` imports it directly.
- **New output kind** → `routes_jobs._KINDS`, `runner.publish()` extension
  list, `prof_builder.BUILDERS`, `registry.OUTPUTS`.
- **New job option** → `store` column (via `_ADDED_COLUMNS`), `submit_job`
  form field, `build_command` (only if the ReportType flag allows it),
  template control shown by the same flag.

---

## 7. Run / test / ship

```bash
.venv/bin/pip install -r requirements.txt -r requirements-web.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env            # APP_USERS, APP_ADMINS, SESSION_SECRET, X_* …
.venv/bin/python profiles/tests/run_all.py     # 8 suites, zero captures
.venv/bin/python -m uvicorn webapp.main:app --reload --port 8000
.venv/bin/python scripts/probe_logged_out.py <url>   # before trusting a platform
docker compose up -d --build     # server; see README "Deploying"
```

Before shipping any change: RULEBOOK checklist (frozen set clean, both
built-in reports run, PDFs opened and looked at, a designed style run once).

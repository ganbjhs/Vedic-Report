# Report Maker — Blueprint (v2.1, August 2026)

One file that describes the whole system precisely enough to rebuild it — or
to hand to a new session and say "redesign this without breaking it". Read
`RULEBOOK.md` alongside; this file says *what is where and how it fits*, the
rulebook says *what will bite you*.

---

## 0. In one paragraph

Give it post links (X/Twitter or Facebook); it screenshots every post in a real
browser and lays the screenshots into a PDF + DOCX (+ HTML for some styles).
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
├───────────────────────────────────────────────────────────────────────┤
│ builders (results.json + PNGs → documents)                            │
│   src/report_builder.py [FROZEN]  influencer/inf_report_builder.py [FROZEN]│
│   profiles/prof_builder.py  (PDF/DOCX/HTML from a profile JSON)       │
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

profiles/                  the profile engine — presentation as data
  registry.py              load/merge/validate profile JSON; ENGINES; user dir
  registry/*.json          shipped profiles: twitter, influencer (parity oracles),
                           client-deck, contact-sheet, twitter-hi-res, facebook
  netlinks.py              which links belong to which platform; platform-neutral
                           row reader (reuses input_loader helpers read-only)
  layout.py                page geometry → placements
  shapes.py                pure-PIL image ops (fit/pad/round/border/shadow)
  progress.py              the stdout vocabulary the web runner parses
  prof_runner.py           workers/retry/quality/results.json (own CTX_KWARGS)
  prof_worker.py           imports the engine DIRECTLY (x | influencer | facebook)
  prof_builder.py          PDF/DOCX/HTML from profile + results.json
  tpl_builder.py           the same for TEMPLATE styles (designed PNG pages + slots)
  thumbnails.py            renders the style thumbnails from real geometry
  run_profile.py           entrypoint mirroring run.py's CLI
  tests/run_all.py         7 zero-capture suites (parity, dispatch, inputs, …)

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
  routes_extras.py         /api/presets /api/styles
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
4. **Publish** newest `reports/*.{pdf,docx,html,xlsx}` + `screenshots.zip` →
   `out/`; skipped links get a plain-English reason from `_STATUS_REASON`.
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
| `ReportType(slug,label,argv,worker_pool,allows_worker_choice,allows_keep_engagement,platform,custom,…)` | report_types.py | form controls, build_command |
| Platform ↔ engine: `ENGINES[engine]["platform"] == profile.platform` | registry.validate | wrong link filter for a style |
| Canonical upload = `Account | Link` sheet | uploads.write_canonical_xlsx | frozen loader reads exactly those headers |
| Static assets linked as `?v=<sha1 of css+js>` | main.py `asset_v` | browsers pair old CSS with new HTML |

---

## 5. Front-end model (for a redesign)

Shell: top bar (brand · theme · avatar menu) · left nav (New report N,
History H, Report styles S; **Admin**: Accounts & sessions, Settings, budget
meter — only when `is_admin`) · main. No right rail. Everything user-facing is
server-rendered Jinja + `app.js` page inits (`initSubmitForm`, `initJobPage`,
`initHistory`, `initStyles`); state lives in the DOM and in the API, never in
a front-end store. Tokens live at the top of `app.css` (light + dark).

New report = Platform pills → compact style rows (Sample opens a modal with
the readable page) → Links (file / paste / sheet tabs + preview table) →
capability-driven options → sticky action bar (name · summary · Save preset ·
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
cover/end), uploads them at /styles, drags screenshot slots and text slots
(title/date/page/account/link/category/metrics) on the page. Stored as a
normal profile with a `template` section (`registry._validate_template`);
assets in `data/profiles/assets/<slug>/`, copied into the job dir with the
profile. `layout.placements` fits screenshots into slots (never crops);
`tpl_builder` paints background + slots. PDF/HTML exact; DOCX approximate.

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
.venv/bin/python profiles/tests/run_all.py     # 7 suites, zero captures
.venv/bin/python -m uvicorn webapp.main:app --reload --port 8000
.venv/bin/python scripts/probe_logged_out.py <url>   # before trusting a platform
docker compose up -d --build     # server; see README "Deploying"
```

Before shipping any change: RULEBOOK checklist (frozen set clean, both
built-in reports run, PDFs opened and looked at, a designed style run once).

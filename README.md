# VedicReport — X / Facebook / Instagram report automation

> **v3.0-a (Aug 2026): projects.** The dashboard is now a workspace of
> *projects* — pick one in the left bar (or make a new one), give it the styles
> it prints in (one or more, each with its own PDF / DOCX / PPTX choice and an
> optional page background), and every run belongs to it. Numeric styles now
> export a **PPTX** too. Read `docs/v3-plan.md` for what comes next (Sources
> that stay in sync, the per-project API key, Grok Studio).

Give it a list of X/Twitter post links; it screenshots every post in a logged-in
browser and builds a **PDF + Word (.docx)** report — or, for a designed-page
(Canva) style, a **PDF + an editable PowerPoint deck (.pptx)**.

> Changing or rebuilding this project? Read **[RULEBOOK.md](RULEBOOK.md)** first —
> it collects the constraints and traps that are not obvious from the code.

Two ways to use it:

* **Web app ("Report Maker")** — colleagues sign in, pick a platform and a
  report *style*, add links (file, paste or Google Sheet), check the preview,
  generate, download. This is the main interface.
* **CLI** — the original command-line tool, unchanged.

The web app's dashboard (`webapp/`, no build step — one CSS file, one JS file):

* **New report** — Platform → Style → Links, with a live preview table of what
  will be captured (duplicates and rejected rows called out by row number), a
  rough time estimate, an **Outputs** row listing exactly the formats the chosen
  style produces (all ticked — untick one and it is not built), and a sticky bar
  with the report name and Generate.
* **Report styles** — a gallery of every style with its real page thumbnail,
  plus a **style designer**: page size, grid, image box, aspect/padding, corner
  radius, border, shadow, cover, footer, per-post fields, outputs. The preview
  is drawn by the same code as the cards, and a saved style is validated by
  `profiles/registry.py` before it can be selected. Saved under
  `data/profiles/`; the two built-in reports cannot be edited from here.
* **Presets** — remember a platform + style + options (+ a Google Sheet URL)
  and re-run with one click.
* **History** — every job with status, counts and download links; a job page
  with live progress, activity log and the "not included" list.
* **Accounts & sessions**, **Settings** (read-only view of `.env`), capture
  budget meter, health pills, light/dark theme, `N` / `H` / `S` shortcuts.
* **Facebook** — a third capture engine (`facebook/fb_capture.py`) behind the
  Facebook pill. Public posts are captured **without any account**: the login
  dialog, dim backdrop, cookie banner and bottom bar are removed first, the post
  is framed from its author line to its Like · Comment · Share row (comments
  excluded), and links can be `/posts/`, `/photos/`, `/videos/`, `photo.php`,
  `permalink.php`, `/watch`, `/reel/`, `/share/p/` or `m.facebook.com` forms.
  If you ever need a signed-in capture, save a Playwright storage state at
  `sessions/fb_state.json` and it is used automatically. Try any link first with
  `python scripts/probe_logged_out.py <url>`.

* **Instagram** — fourth engine (`instagram/ig_capture.py`), public posts
  logged-out: the sign-in panel is closed, then the post's `<article>` is
  framed. `sessions/ig_state.json` is used if you save one.
* **Combined report** — the way the team actually reports: one sheet
  (`Section | Handle | Link | Like | Post Impression | Video Views | Reach…`)
  with X, Facebook and Instagram links mixed, in sections. The right engine
  runs per link, sections are kept, and the **metrics print from the sheet's
  columns** (Insights numbers are not public, so nothing is scraped). The
  shipped style **Combined report (16:9)** produces cover → summary table of
  sections → one landscape page per post (handle, section, date, platform
  logo, *Post i / Top N posts*, metric pills, LINK button, screenshot) → links.
  Designers duplicate it, drop their own Canva art (16:9 PNG, art only) and
  keep the slots — or press *Place standard slots* on any page image.

Why X still uses an account when Facebook does not: X shows a single post
logged-out, but not reliably the *conversation* the Twitter report is defined
by (a reply shot together with its parent), hides sensitive/age-gated media,
throws a sign-up sheet after the first scroll, and rate-limits anonymous IPs
far harder — every one of those cost a day before the shared account existed
(RULEBOOK rules 6, 19–21). Facebook's public Page posts do render for a
logged-out desktop visitor once the login sheet is removed, so that engine
starts account-free; if Facebook begins walling those too, `fb_state.json` is
the switch.

Two kinds of report:

| | **Twitter Report** | **Influencer Report** |
|---|---|---|
| Screenshot | tweet with the engagement bar **cropped out** (optional tick keeps it — see below); a reply is shot together with its parent post | keeps username, text, media **and** likes/reposts |
| Metrics | none | Followers, Reactions, Comments, Reach, Shares |
| Layout | one post per page, letter | **two posts per page**, A4 |
| Ends with | links table | links table |

---

## Table of contents

1. [Quick start (local)](#quick-start-local)
2. [How it works](#how-it-works)
3. [Designing a style in Canva](#designing-a-style-in-canva)
4. [Project structure](#project-structure)
5. [The input file](#the-input-file)
6. [Configuration](#configuration)
7. [Deploying on your own server](#deploying-on-your-own-server)
8. [Operations and troubleshooting](#operations-and-troubleshooting)
9. [Design notes](#design-notes)

---

## Quick start (local)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-web.txt
.venv/bin/python -m playwright install chromium

cp .env.example .env          # set APP_USERS and SESSION_SECRET
python save_login.py x        # one-time: log into X by hand in the window that opens

.venv/bin/python -m uvicorn webapp.main:app --port 8000
```

Open <http://127.0.0.1:8000>.

Locally the defaults apply: a **local Chromium** and a **background job queue**.

### The CLI, if you prefer it

```bash
python run.py config/links.xlsx --title "Twitter Report"    # Twitter report
python influencer/run_influencer.py links.xlsx              # Influencer report
python run.py -                                             # paste links on stdin
```

Flags: `--title`, `--date dd-mm-yy`, `--no-date`, `--workers N`, `--headed`,
`--keep-engagement`. Output lands in `reports/`.

The header reads `"<title> <date>"` and the file is named `<Title>_<date>`. Pass
`--no-date` and both become the title verbatim — which is what the web app does,
so a report named *July Fake Accounts* has exactly that at the top of the page
and downloads as `July Fake Accounts.pdf`.

### Keeping the engagement line (Twitter report only)

By default the Twitter report cuts above the `time · views` line, so no counts
appear anywhere in the picture. Tick **"Keep the engagement line in the
screenshot"** on the form (or pass `--keep-engagement`) and the cut moves to
just below the action bar instead. On a comment link that keeps *both* lines:

```
parent name/@handle + text + media + like/views
    -> comment text + media + like/views
```

The tick is off by default and only exists for the Twitter report — the
influencer capture already keeps engagement in frame, so the option is hidden
when that type is selected.

### Capture speed (how many browsers)

The Twitter report's form has a **Capture speed** picker: how many Chromium
workers capture the batch in parallel. Leave it on *Server default* (`WORKERS`
in `.env`) unless a batch is unusually large. The picker stops at `MAX_WORKERS`,
and the server clamps anything above that — the limit is not decoration, each
browser is 0.5–1 GB and an overshoot is an out-of-memory kill, not an error
message.

Like the crop tick, the picker is **Twitter-only** and hidden for the Influencer
report, which stays pinned to `INFLUENCER_WORKERS` (see the table below for
why).

**More browsers only help if there are spare CPU cores.** Chromium spends the
capture decoding each post's images and video, so on a 1-vCPU server three
browsers take turns on one core: same wall-clock, triple the memory. Set
`MAX_WORKERS` to roughly your vCPU count, and measure before raising it:

```bash
nproc                                   # cores you actually have
free -g                                 # free RAM
docker stats --no-stream                # what a running capture costs
```

Two other ceilings worth knowing: workers are capped at the number of links
(8 workers on 5 links runs 5 browsers), and every worker signs in as the *same*
X account — push it hard enough and you trade speed for rate-limit retries.

---

## How it works

```
 Browser                    App (FastAPI)                     Browser engine
 ───────                    ─────────────                     ──────────────
 sign in  ──────────────▶  session cookie, CSRF, rate limit
 upload file ───────────▶  parse + validate + normalise to .xlsx
 Generate ──────────────▶  create job, isolated working dir
                                  │
                                  ├─ subprocess: run.py  or  run_influencer.py
                                  │        │
                                  │        └── Playwright ──▶ Chromium
                                  │              (local, or remote over CDP)
                                  │
                           progress parsed from the pipeline's stdout
 live status ◀──────────  NDJSON stream / polled JSON
 download   ◀──────────  PDF · DOCX / PPTX · screenshots.zip
```

**The process, step by step:**

1. **Upload.** Any of `.xlsx .xls .csv .tsv .txt`. The web layer parses it,
   validates that it contains X links, counts them, and rewrites it as a
   canonical `.xlsx`. Bad files are rejected here, before a job exists.
2. **Job creation.** Each submission gets its own directory under `data/jobs/`
   containing a private copy of the pipeline code, so concurrent jobs cannot
   collide.
3. **X sign-in.** Before capture, the app makes sure a valid X session exists —
   restoring it from the session store, or signing in headlessly with the shared
   capture account.
4. **Capture.** The pipeline runs as a subprocess, exactly as the CLI does. It
   screenshots each post, retries failures, and re-captures blank/black shots.
   Before every shutter it clears X's dialogs, sheets and dim backdrop off the
   post; a reply is shot together with its parent, and the frame is checked
   against that promise before it is accepted.
5. **Build.** Screenshots are JPEG-compressed and assembled into the formats
   you ticked — PDF + DOCX for the numeric styles, PDF + PPTX for a
   designed-page one. The document header is exactly the name you typed — no
   date, no decoration.
6. **Deliver.** The documents and a ZIP of all screenshots, named after the report
   and scoped to the session that created the job. Links that failed are listed
   in the activity log with a reason, and left out of the document — including
   posts X age-restricted behind mobile-app verification, which a desktop
   browser cannot get past.

---

## Designing a style in Canva

A report style is a page you design — anywhere that exports a PNG — with
*slots* the app prints into. Nothing here needs code, and the app draws the
guide for you so nothing has to be measured by hand.

**1 · Start from a style, not from zero.** Report styles → any designed style →
**Make my own version**. Its slots, logo, summary box and text arrive in the
designer with the original art greyed out behind them, and a banner: *replace
the page art, keep the slots*. (Starting from a blank page instead? Upload your
PNG and press **Place standard slots**.)

**2 · Download the Canva guide.** Press **⬇ Download Canva guide**. You get a
transparent PNG at exactly the page's pixel size — 1920 × 1080 for a 16:9
slide, 1440 × 1080 for 4:3, A4/Letter at 150 dpi — with every slot drawn as a
labelled outline.

**3 · Design underneath it.** In Canva, create the design at the size the note
under the button tells you (for slides: *Presentation (16:9)*). Upload the
guide, put it on top, lock it, and build the art beneath: backgrounds, pills,
frames, brand marks. **Art only** — no post numbers, no account names, no
platform logos and no metric values; the app prints those into the slots, and a
number painted into the art would be wrong on every other page.

**4 · Export and drop it back.** Delete the guide layer, **Share → Download →
PNG**, then drag that file onto the designer's page tab. The paper size follows
the image, so a slide is never squashed onto A4.

**5 · Preview before you save.** Press **👁 Preview page**. The server renders
one real page beside the editor — your art, a sample screenshot, sample values
(*Kashi Ke Wasi*, Like 676, Impressions 63,900, LINK) — and refreshes as you
drag. Nudge boxes with the arrow keys (Shift = 10 px), duplicate with ⌘/Ctrl+D,
or type exact X/Y/W/H percentages. Boxes snap to each other and to the page
centre.

**6 · Save, then get it approved.** Name the style and **Save style**. It stays
*pending* until an admin approves it on the Report styles page; then it appears
on New report like any other style.

**What you get out of it.** A designed style produces **a PDF and a PPTX**, the
same page twice. The PDF is the print-ready one. The PPTX is the same slide with
nothing flattened: the art is the background picture, and every screenshot, every
value, the LINK button and the summary table are separate objects you can move,
retype or restyle — in PowerPoint, in Keynote, or after uploading to Google
Slides. There is no Word version of a designed style, and no links page at the
end: each post carries its own LINK.

**Optional — your own fonts.** Upload up to three `.ttf`/`.otf` files (≤2 MB
each) and pick one per text slot. They travel with the style into every job and
are **embedded** in the PDF. A .pptx can only ask for a font by name, so the
deck names the family read out of your file — on a machine that does not have
it installed, PowerPoint substitutes.

**Then look at the first PDF.** The preview is drawn from the same geometry as
the document, but it is a picture of one page — open the first real report and
check it (this project's rule 3, and it has been earned).

---

## Project structure

```
├── run.py                    CLI entrypoint — Twitter report
├── save_login.py             one-time manual X login
├── install.py                dependency + browser setup
│
├── src/                      the X capture pipeline (frozen — see Design notes)
│   ├── run_report.py         orchestration: workers, retries, quality pass
│   ├── _worker.py            one browser per worker process
│   ├── input_loader.py       reads .xlsx / .csv / pasted lists
│   ├── platforms.py          X constants + login helpers
│   ├── shot_quality.py       detects blank / black / half-loaded screenshots
│   ├── report_builder.py     builds the Twitter PDF + DOCX
│   ├── save_sessions.py      manual login flow
│   └── capture/x_capture.py  the X capture algorithm
│
├── influencer/               the Influencer report (parallel to src/)
│   ├── run_influencer.py     CLI entrypoint
│   ├── inf_runner.py         orchestration
│   ├── inf_worker.py         parallel worker
│   ├── inf_capture.py        capture keeping engagement + reading metrics
│   └── inf_report_builder.py A4 PDF + DOCX, two posts per page
│
├── profiles/                 the profile engine (styles as JSON) — see docs/profile-engine.md
│   ├── tpl_builder.py        PDF + editable PPTX for designed-page (Canva) styles
│   └── tpl_preview.py        the design kit: Canva slot guide + one-page preview
├── facebook/                 the Facebook capture engine (public posts, logged-out) — via profiles/
├── instagram/                the Instagram capture engine (public posts, logged-out) — via profiles/
│
├── webapp/                   the web layer
│   ├── main.py               app, pages, auth routes
│   ├── config.py             environment / .env settings
│   ├── auth.py               sessions, CSRF, login rate limiting
│   ├── uploads.py            upload parsing, validation, normalisation
│   ├── sheets.py             Google Sheets (published CSV) input
│   ├── report_types.py       platforms + report types as a capability table
│   ├── health.py             per-platform session health + capture budget
│   ├── previews.py           style thumbnails for the cards
│   ├── styles.py             the style designer (user profiles under data/profiles/)
│   ├── x_login.py            headless X sign-in for the shared account
│   ├── routes_jobs.py        preview / submit / status / cancel / download
│   ├── routes_extras.py      presets + styles JSON APIs
│   ├── jobs/
│   │   ├── store.py          SQLite job records
│   │   ├── queue.py          bounded worker pool
│   │   ├── runner.py         job dir, subprocess, progress, artifacts
│   │   └── cleanup.py        retention sweep
│   ├── templates/            server-rendered pages
│   └── static/               CSS + JS (no build step)
│
├── Dockerfile                the deployable image (Chromium included)
├── docker-compose.yml        app + Caddy (HTTPS), with persistent volumes
├── Caddyfile                 your domain, automatic HTTPS
├── requirements.txt          CLI dependencies
├── requirements-web.txt      web app dependencies
│
├── config/links.xlsx         default input list
├── sessions/x_state.json     the X login cookie — SECRET, gitignored
├── reports/                  CLI output
└── data/                     web app runtime state — gitignored
```

---

## The input file

Accepted: `.xlsx`, `.xls`, `.csv`, `.tsv`, `.txt`.

* A column of X/Twitter post URLs. If a header row names the columns
  (`link` / `url` / `post link`, optionally `account` / `handle` / `category`),
  those are used; otherwise any cell containing a URL is treated as a link.
* A non-URL row becomes a section heading (category) for the links beneath it.
* Non-X links are skipped with a note.

Order is preserved throughout — the document follows the file.

---

## Configuration

Everything is environment variables, loaded from `.env` locally. See
[`.env.example`](.env.example) for the full annotated list.

**App access**

| Variable | Default | Notes |
|---|---|---|
| `APP_USERS` | — | `alice:pw1,bob:pw2`. A bcrypt hash is detected automatically. |
| `APP_USER` / `APP_PASS` | — | Alternative single-account form. |
| `APP_ADMINS` | (everyone) | Comma-separated usernames who see Accounts & sessions, Settings, the style designer, health and the budget meter. Everyone else gets New report / History / Report styles only. |
| `SESSION_SECRET` | — | Long random string. Changing it signs everyone out. |
| `COOKIE_SECURE` | `0` | Set `1` when served over HTTPS. |

**The shared X capture account** — lets the server sign in by itself

| Variable | Notes |
|---|---|
| `X_USERNAME` | the @handle, without the `@` |
| `X_PASSWORD` | its password |
| `X_EMAIL` | X asks for this on logins from unfamiliar machines |
| `X_TOTP_SECRET` | only if that account has 2FA on |

**Work**

| Variable | Default | Notes |
|---|---|---|
| `WORKERS` | `3` | Browsers per job, when nobody picks. One per 1–1.5 GB of free RAM. |
| `MAX_WORKERS` | `4` | Ceiling for the form's Capture speed picker (Twitter only). Match it to vCPUs. |
| `MAX_CONCURRENT_JOBS` | `1` | Worst-case total browsers = this × `MAX_WORKERS`, since a job may pick above `WORKERS`. |
| `INFLUENCER_WORKERS` | `1` | Browsers for the Influencer report, always — the picker cannot override it. Its follower-count cache is per worker process, so extra workers re-fetch the same profiles. |
| `EXECUTION_MODE` | `queue` | Background workers. `inline` exists only for hosts that stop the CPU after a response. |
| `MAX_LINKS` | `200` | Per job. |
| `MAX_UPLOAD_MB` | `5` | |
| `JOB_TIMEOUT_MINUTES` | `90` | |
| `RETENTION_DAYS` | `7` | Old jobs are deleted automatically. |

---

## Deploying on your own server

Runs on any always-on Linux box with root — a **Hostinger VPS (KVM)** is what
this is written for. Not shared/web hosting: that cannot run Chromium or Docker.

**Size it by RAM.** A browser costs ~0.5–1 GB, so allow one worker per 1–1.5 GB
free. 4 GB is a sensible floor (`WORKERS=3`); 8 GB is comfortable (`WORKERS=5`).

One container runs the web app, the background workers and Chromium. Caddy sits
in front for automatic HTTPS. Nothing else, and no external services.

### 1. Prepare the server

```bash
ssh root@<VPS_IP>
apt update && apt -y upgrade
apt -y install docker.io docker-compose-plugin git
systemctl enable --now docker
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
```

### 2. Point a domain at it

In Hostinger hPanel → DNS, add an **A record** pointing your subdomain at the
server. For this deployment that is already done:

```
report.vedictech.in  →  200.97.175.12
```

`Caddyfile` is pre-configured for that hostname.

### 3. Get the code and configure it

```bash
git clone <YOUR_REPO_URL> app && cd app
mkdir -p sessions data reports

# The container runs as UID 1000. Deploying as root leaves these folders
# root-owned, the app cannot create its job directories, and it exits at
# startup with "PermissionError: /app/data/jobs" — which surfaces as a 502.
chown -R 1000:1000 sessions data reports

cp .env.example .env
nano .env
```

Set at minimum:

| Variable | Value |
|---|---|
| `APP_USERS` | `alice:pw1,bob:pw2` — one pair per colleague |
| `SESSION_SECRET` | `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `X_USERNAME` / `X_PASSWORD` / `X_EMAIL` | the dedicated X capture account |
| `WORKERS` | `3` on 4 GB, `5` on 8 GB |
| `COOKIE_SECURE` | `0` for now; `1` once HTTPS works |

### 4. Seed the X login

The first sign-in is easiest done by hand, because X may show a CAPTCHA to a
brand-new server IP. On **your own computer**:

```bash
python save_login.py x                    # a browser opens; sign in
scp sessions/x_state.json root@<VPS_IP>:~/app/sessions/x_state.json
```

After that the server refreshes the cookie itself whenever it expires, using the
`X_*` credentials. If you skip this step it will simply sign in on first use.

### 5. Start it

```bash
docker compose up -d --build
```

The app listens on `127.0.0.1:8000` — not exposed to the internet directly.
Give it a public HTTPS address with **one** of the following.

**A. The server already runs nginx (another site is on it)**

Do not enable Caddy — two servers cannot both bind port 80, and stopping nginx
would take the other site down. Add a server block instead:

```bash
cat > /etc/nginx/sites-available/report.vedictech.in <<'EOF'
server {
    listen 80;
    server_name report.vedictech.in;

    client_max_body_size 10M;          # uploads

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # A capture takes minutes; do not cut the connection short.
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
        proxy_buffering       off;     # keeps live progress flowing
    }
}
EOF

ln -sf /etc/nginx/sites-available/report.vedictech.in /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

apt -y install certbot python3-certbot-nginx
certbot --nginx -d report.vedictech.in --redirect
```

`nginx -t` validates before reloading, and certbot only edits the block for this
hostname — the other site is untouched.

**B. Nothing else on ports 80/443**

```bash
docker compose --profile caddy up -d
```

The first build takes several minutes (it pulls the Playwright image). Then:

```bash
docker compose ps            # expect "running (healthy)"
docker compose logs -f web
```

Open <https://report.vedictech.in> and sign in.

> **Testing on a bare IP instead?** Comment out the `caddy` service in
> `docker-compose.yml`, change the web service's port line to `"8000:8000"`,
> and use `http://<VPS_IP>:8000`. Keep `COOKIE_SECURE=0` while you do.

### 6. Check it

Open **X login status** in the header — it should say *Signed in*. Then run a
Twitter Report and an Influencer Report and confirm the downloads.

Reboot the server once (`reboot`) and confirm it comes back on its own —
`restart: unless-stopped` handles that.

---

## Operations and troubleshooting

| Task | Command (on the server, in `~/app`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f web` |
| Update after a code change | `git pull && docker compose up -d --build` |
| Restart | `docker compose restart` |
| Change a password | edit `APP_USERS` in `.env`, then `docker compose up -d` |
| Rotate the X account | edit `X_USERNAME`/`X_PASSWORD`, `docker compose up -d`, then press **Sign in to X now** |
| Expired X session | handled automatically — the server signs in again |
| OS updates | `apt update && apt -y upgrade` occasionally |

| Symptom | Fix |
|---|---|
| 502 from nginx, container `Restarting` | `docker compose logs web`. If it says `PermissionError: /app/data/jobs`, run `chown -R 1000:1000 data sessions reports` |
| Job dies, container restarts | out of memory — lower `WORKERS`, or add RAM |
| Captures fail on media-heavy posts | make sure `shm_size: "1gb"` is still in `docker-compose.yml` |
| Login page loops back to itself | `COOKIE_SECURE=1` without HTTPS — set `0`, or finish the Caddy step |
| "nobody can log in" | `APP_USERS` unset or malformed; needs `user:pass,user2:pass2` |
| HTTPS certificate not issued | DNS A record not pointing here yet, or ports 80/443 blocked |
| Every link hits a login wall | open **X login status**; the last sign-in error is shown there |
| Links come back `login_wall` | open **X login status**; the last sign-in error is shown there |
| Screenshot looks wrong | X may have changed its DOM — see the crop notes in `src/capture/x_capture.py` |

---

## Design notes

**The X pipeline is treated as frozen.** It was tested in production before the
web app existed, so the web layer *invokes* it rather than rewriting it. The
It is currently **byte-for-byte identical to its originally tested state** —
`git diff` against the first commit over `run.py`, `src/`, `install.py` and
`requirements.txt` is empty. A temporary two-line change existed while the app
ran on a remote browser service; that has been reverted.

**Job isolation copies code, not just cwd.** `src/run_report.py` anchors its
output with `Path(__file__).resolve().parents[1]` — the *file's* location, not
the working directory, and `.resolve()` collapses symlinks. Changing `cwd` would
therefore not redirect anything, and concurrent jobs would collide in one
`reports/`. So each job gets its own physical copy of the code (~90 KB, a few
milliseconds) with `sessions/` symlinked to the one real cookie.

**Progress comes from stdout.** The pipeline already prints what it is doing, so
the runner parses those lines rather than instrumenting the pipeline.

**Uploads are normalised before use.** The frozen `input_loader` mishandles two
formats — an `.xls` fails the zip check and gets read as text, and a `.tsv` is
parsed with csv's *comma* dialect so the link ends up glued to the account name.
The web layer converts every upload to a canonical `.xlsx` first, which fixes
both without touching the loader. The layout logic is not reimplemented: the
normaliser hands its grid to the frozen `input_loader._rows_from_grid`.

**The Influencer report is a parallel implementation**, not a fork. It mirrors
the proven structure but crops *below* the engagement bar, picks the correct
article on reply pages (where the first article is the parent tweet, not the one
you linked), waits for video poster frames, and reads metrics from the action
bar's `aria-label`. `inf_worker.py` imports `inf_capture` directly, so neither
`src/_worker.py`'s routing nor the capture dispatcher needed to change.

**Why the browser is remote in production.** Free hosts without a credit card
cap RAM near 512 MB, which cannot run Chromium at all. All capture logic
operates on a Playwright `page`, so it is identical whether the browser is local
or reached over CDP — only where the browser lives changes.

**Security.** `sessions/x_state.json` and `.env` are never served, never sent to
the browser, and never baked into the image. Uploaded filenames are display-only;
the report name is sanitised before touching the filesystem. Downloads are
resolved through the job record, re-checked to be inside that job's folder, and
matched against the session owner. Upload size and link count are capped, and
files must parse before a job is created.

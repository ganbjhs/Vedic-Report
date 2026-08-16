# Rulebook — things to know before changing this project

Every rule here comes from something that actually broke. If you are redesigning
or extending this project, read this first; it will save you the day it cost the
first time.

[README.md](README.md) explains *what the project is and how to run it*. This
file explains *what will bite you*.

---

## 1. The golden rule: the X pipeline is frozen

`run.py`, `src/run_report.py`, `src/report_builder.py`, `src/capture/`,
`src/input_loader.py` and friends were tested in production before the web app
existed. **Invoke them; do not rewrite them.**

`src/` was byte-for-byte identical to its originally tested state until the
capture-quality work below. `input_loader.py`, `report_builder.py`,
`platforms.py` and `save_sessions.py` still are; check what you are about to
touch:

```bash
git diff <first-commit> -- run.py src/ install.py requirements.txt
# expected: run.py, src/overlays.py, src/capture/x_capture.py,
#           src/capture/__init__.py, src/shot_quality.py, src/run_report.py,
#           src/_worker.py
```

(An earlier two-line change to `src/_worker.py`, from when the browser ran
off-box, was approved at the time and has since been reverted. The line there
now is approved edit 4's.)

**Approved edit 1 — `src/capture/x_capture.py` shoots a reply together with its
parent**: parent name/@handle + text + media, then the reply's text and media,
engagement bars hidden throughout. This could not be done from outside: the crop
is chosen inside the capture, and the frozen dispatcher hands it the page. Its
blast radius is the frame only — the result dict, the runner and the builder are
untouched, and a non-reply post still comes out exactly as before.
`_THREAD_ANCESTORS` sets how far up the thread the frame reaches (1 = the
parent). Everything the change relies on is written up in rule 6.

**Approved edit 2 — overlays and the framing check** (`src/overlays.py`, plus
its callers in both captures, `shot_quality.py` and `run_report.py`). Same
argument: a dialog has to be taken off the page *before* the shutter, and only
the capture holds the page. See rule 19 for what it fixes and why it is one bug,
not two.

**Approved edit 4 — `--keep-engagement`** (`src/capture/x_capture.py`,
`src/capture/__init__.py`, `src/_worker.py`, `src/run_report.py`; `run.py` only
in its docstring, since an unknown `--flag` already falls through to the
runner). Other reports now need the counts *in* the picture, so the crop is a
choice instead of a constant: the cut moves below the focused post's action bar
and ancestors keep theirs, giving `parent + like/views -> comment + like/views`.
This cannot be done from outside `src/` — the pixels below the cut never exist
in the PNG, and the clip is chosen inside the capture. Additive and default-off:
without the switch, `_crop_box` runs the same branch it always did and the
picture is unchanged. The flag rides on the task dict, so `run_chunk`'s pickled
signature did not move.

**Approved edit 3 — `run.py --no-date`** (mirrored in
`influencer/run_influencer.py`). The web app needs the document header to read
exactly what the user typed, and `run.py` composes `"<title> <date>"` internally
where no caller can reach it. Additive: without the switch every existing
invocation behaves as before.

**Approved edit 5 — a reply must not ship without its parent**
(`src/capture/x_capture.py`, `src/run_report.py`, plus the reason text in
`webapp/jobs/runner.py`). See rule 20 for the full write-up. In short: the guard
that was supposed to prevent this was gated on X rendering a "Replying to" line,
and X omits that line in precisely the case the guard exists for, so it declined
to act on its own target. Measured at 4/60 reply captures lost on a healthy
session and 38/60 on a degraded one — all silent, all `status="ok"`. Cannot be
fixed from outside `src/`: the ancestor is chosen inside the capture, and only
the capture holds the page. Additive — every new parameter defaults to the old
behaviour, and a genuine root post takes the identical early return (verified:
zero extra scrolls, zero extra waits on that path).

Before you edit anything under `src/`, ask whether you can get the same result
from outside it. You almost always can — see rules 2 and 8 for two cases where
that looked impossible and wasn't.

---

## Dead ends — do not retry (append-only)

**Read this before proposing anything.** Every line below was actually tried and
actually measured in this project, and every one of them is the kind of idea
that looks obviously correct on a first reading of the code. A future session —
human or AI — that suggests one of these is not being clever; it is repeating
work that has already been paid for. If you think one deserves revisiting, bring
the measurement that overturns the one recorded here.

Append to this list whenever something is tested and rejected. Never delete a
line: an idea that was wrong once will look attractive again.

| Dead end | Why not | Do instead |
|---|---|---|
| **DPR 2 as the global default** (`device_scale_factor` in `src/run_report.py`'s `CTX_KWARGS`) | 4-run benchmark: resolution genuinely doubles (598 -> 1196 px, 122 -> 244 DPI) but parent loss roughly **doubles** with it, 3.5/20 -> 6.5/20 with no overlap between conditions. It also costs +24% wall clock. Memory is *not* the problem the brief predicted (+9.6%/worker) | Ship 2x as an **opt-in high-res profile** owning its own `CTX_KWARGS`. Costs are then paid only by jobs that asked. See rule 20 and `docs/profile-engine.md` §10 |
| **Monkey-patching `x_capture._THREAD_ANCESTORS`** from a profile runner | It *works* — the constant is read at call time — but it is an invisible hand reaching into frozen module state, which is exactly the surprise rule 1 exists to prevent. Nothing currently needs it | Ask for a proper approved edit adding `capture(..., thread_ancestors=None)`, the `--keep-engagement` pattern. Until then the knob does not exist, and `profiles/registry.py` rejects it by name |
| **Tuning the retry budget to fix parent loss** (more attempts, longer backoff in `_ensure_parent`) | Instrumented over 120 reply captures: **42 of 42 losses never reached the remount at all**. The bail-out was the bug; the retry budget was never the bottleneck | Check whether a guard's *precondition* is ever true before tuning what happens after it (rule 20) |
| **Byte- or pixel-identical PDF diffing** to prove a builder change is safe | reportlab embeds timestamps, so byte-identity is impossible; rasterising to compare pixels needs a dependency this project does not have | **Geometry parity**: assert page count and every `(page, x, y, w, h)` placement against the frozen builders' own functions and constants. `profiles/tests/test_parity.py` |
| **Trusting `frame_ok`, `status="ok"` or `[verify] N/N clean`** as evidence a run was good | All 80 shots across four benchmark runs reported `frame_ok=True`, `overlay=False`, `status="ok"` and `[verify] 20/20` — while shots were visibly missing their parent post, and one showed a loading spinner instead of a video | Rule 3, without exception: **open the images and the documents and look**. A green status only proves the code did not raise |
| **"Fixing" the absolute paths in `reports/results.json`** | They look like a portability bug and are not. Rule 2 copies the code into the job dir, so `ROOT` resolves inside the job and the paths are self-consistent | Leave them. A consumer **inside** the job subprocess may trust them; a consumer in the **webapp** must glob or rebase, as `publish()` and `_zip_screenshots()` already do |
| **Running more than ~300 captures a day on one X account** | Measured: after ~320 the same 60-link set went from 0 to 18 retries, 1 to 8 recaptures, produced 598x80 frames, and parent loss hit 38/60 (63%). Nothing raised an error | Rule 21. Rest the account, and treat any run with an unusual retry count as inadmissible rather than as a result |
| **Averaging a healthy run with a degraded one** | The two zero-cost diagnostic passes were 6.7% and 63.3%. Their mean, 35%, describes neither and would have been reported as the bug's rate | Report the conditions separately, or discard the degraded run and re-measure |
| **Cropping a capture to a tile aspect** (`fit: "crop-top"` in a profile) | Caught by eye on the first `contact-sheet` render: a master is *parent + reply*, and a 1:1 crop kept the parent and threw the reply away. That is rule 20's bug re-created at the presentation layer, where no capture-side gate can see it | **Pad, never crop.** `contact-sheet` uses `fit: "pad"` at 4:5. The `crop-to-aspect` op still exists because a profile may legitimately want it for non-evidence imagery, but no shipped profile uses it |

---

## 2. Isolation copies code; cwd does nothing

```python
ROOT = Path(__file__).resolve().parents[1]     # src/run_report.py
```

Output paths are anchored to **where the file lives**, not the working
directory — and `.resolve()` collapses symlinks, so symlinking `src/` into a job
folder resolves straight back to the original.

**Consequence:** `cwd=` will not redirect output, and two concurrent jobs would
write into the same `reports/`. Each job therefore gets its own *physical copy*
of the code (~90 KB, milliseconds) in `data/jobs/<id>/app/`.

If you ever "optimise" that copy away, concurrent jobs will silently corrupt
each other's output. The copy is the isolation.

---

## 3. Verify by looking at the artefact, not the status

The most expensive mistake in this project: the Browserless captures were
verified by checking job status (`done`), metrics (real follower counts), and
file sizes (240 KB PDFs). All green. Every screenshot was visually broken —
X's navigation sidebar was in every image.

**Rule: for anything that produces an image or a document, open it.** A green
status only proves the code did not raise.

---

## 4. Remote browsers (CDP) lie about the viewport

> Historical — the app runs a **local Chromium** now. Keep this if you ever move
> the browser off-box again; it cost a full day the first time.

Connecting over CDP is not the same as launching locally.

* `new_context(viewport=…)` is **ignored**. So is `page.set_viewport_size()`.
  Playwright reports the size you asked for while `window.innerWidth` stays at
  the service's default (800 px on Browserless).
* The only thing that works is the service's own launch option:
  `?launch={"defaultViewport":{"width":1500,"height":1600}}`.

**Why it matters:** at 800 px X switches to its narrow layout and paints the
left nav *over* the tweet column. The nav then sits inside the article's
bounding box, so no amount of cropping can remove it. The article moves from
`x=393` to `x=126` — that number is the tell.

**Debug recipe** when a remote capture looks wrong:

```python
page.evaluate("() => ({inner: innerWidth, outer: outerWidth, dpr: devicePixelRatio})")
```

If `inner` is not what you set, nothing downstream will be right.

---

## 5. Chromium ≠ Chrome: codecs

> Also historical, and also the reason a remote browser is painful.

Open-source **Chromium has no H.264/AAC**. X's videos are H.264, so every video
post renders "The media could not be played" instead of a poster frame.

* Browserless default endpoint → Chromium → broken video
* `…browserless.io/chrome?token=…` → real Chrome → works
* Playwright's bundled Chromium *does* include the codecs, which is why it
  always worked locally and only broke in the cloud

**Probe before blaming the site:**

```python
page.evaluate("""() => document.createElement('video')
    .canPlayType('video/mp4; codecs="avc1.42E01E"')""")   # "" means no codec
```

---

## 6. X's DOM: the four things that are not obvious

1. **On a reply URL, the first `article` is the PARENT tweet.** Selecting
   `.first` screenshots the wrong post. Two ways to find the right one, and the
   difference matters:
   * *By identity* — only the linked post's own article contains links carrying
     its status id (timestamp, `/photo/1`, `/analytics`), so
     `article:has(a[href*="/status/<id>"])` names it outright. Scroll it into
     view first: **X unmounts off-screen articles**, and an unmounted post
     cannot be found at all. In a long self-thread it drops out of the DOM
     between one call and the next, which is exactly how a capture ends up
     silently showing the parent alone.
   * *By inference* — score the articles (the focused one has no `<time>` inside
     `[data-testid="User-Name"]`, ancestors do; its action bar reports view
     counts). Needed only for URLs with no readable status id, and it is the
     path that guesses, so never trust a score that the id could have settled.
2. **Engagement is `[role="group"]`.** Crop *above* its top for the Twitter
   report, *below* its bottom for the Influencer report. That one boundary is
   the entire difference between the two reports.
   A parent's action bar sits *between* the parent and the reply, so when the
   Twitter report shoots both in one frame no crop can remove it — it has to be
   hidden (`display:none`) before the shot. Same for X's sticky "← Post" bar:
   scroll a parent up to the viewport edge and that bar paints over its name
   row, which is invisible in the code and obvious in the image.
3. **Metrics live in an `aria-label`**, not in the visible text:
   `"12 replies, 3 reposts, 45 likes, 6 bookmarks, 7890 views"`. One parse gets
   everything; the per-button fallbacks are only for when that is missing.
4. **Follower count is not on the post.** It requires a profile visit, so it is
   cached per handle — and that cache lives in the worker *process* (rule 12).

X changes its DOM without warning. When a capture breaks, check these four
before assuming the code rotted.

---

## 7. Screenshot quality gates exist for a reason

`shot_quality.py` rejects blank/black/half-loaded frames by pixel variance, and
the runner re-captures them. Video posts are the usual offender: shoot too early
and you get a black rectangle.

Do not remove the retry/quality passes to make runs faster. They are why the
output is trustworthy.

**Prefer a DOM fact to a pixel guess.** The gate now runs three checks, in
descending order of confidence (`run_report._why_poor`):

| check | evidence | on failure |
|---|---|---|
| `overlay` | the capture *saw* a dialog still on the post | retake, then **drop the link** |
| `frame_ok` | the clip did not span parent → reply | retake, then warn and keep |
| pixel analyzer | blank / black / dimmed histogram | retake, then keep |

Only the first demotes a link out of the report, because only the first is an
observation rather than an inference. A heuristic that drops links silently
turns a cosmetic false positive into missing evidence — the pixel checks
therefore ask for a retake and never for a deletion.

---

## 8. Change the input, not the builder

Two problems that looked like they needed frozen-code edits, solved from
outside:

* **`.xls` and `.tsv` were broken.** `input_loader` sends every path through
  `load_excel` (an `.xls` fails the zip check and gets read as text), and parses
  `.tsv` with csv's *comma* dialect, gluing the account name onto the URL. Fix:
  the web layer normalises every upload to a canonical `.xlsx` first, then hands
  its grid to the frozen `input_loader._rows_from_grid` so the layout logic is
  not reimplemented.
* **"Tweet Links" printed above every screenshot.** The builder prints a row's
  category, and the builder is frozen. Fix: drop the Category column from the
  canonical sheet. `input_loader` then defaults every row to `"Uncategorized"`,
  and `_has_categories()` returns `False` — which is the exact condition the
  builder uses.

**Pattern:** when a frozen component behaves wrongly, look at what you feed it.

---

## 9. Hosting: this app wants a real server

It now runs on an ordinary always-on VPS with root, which removes every
workaround below. **Requirements: Docker, ≥4 GB RAM, a persistent disk.** Shared
or "web" hosting cannot run Chromium.

The free-tier detour is worth remembering only so nobody repeats it:

| Host | Verdict |
|---|---|
| **Own VPS (Hostinger KVM, or similar)** | what this uses. No caps, real disk, real background workers |
| Oracle Cloud Always Free | free forever, always-on — card needed, ARM capacity often unavailable |
| Google Cloud Run | scale-to-zero — card needed, 60-min request ceiling |
| Render free + Browserless free | no card, but 512 MB, sleeps, no disk, and a browser-minute cap |
| Hugging Face Spaces | **Docker requires a paid plan** since July 2026 |
| Vercel / serverless functions | cannot run a minutes-long backend |
| Railway | trial credits only |

Free hosts forced two ugly adaptations, both since reverted: an external cookie
store (no disk) and running the capture inside the HTTP request (the CPU stops
once a response is sent). If you ever go back to such a host, those are the two
things you will have to rebuild — `EXECUTION_MODE=inline` still exists for the
second.

---

## 10. Memory: measure, do not guess

Measured with the browser *off-box*, so this is the app alone:

| Report | App memory |
|---|---|
| 5 links | 366 MB |
| 20 links | 413 MB |

On your own server add roughly **0.5–1 GB per Chromium worker** on top. That is
where `WORKERS` comes from: one per 1–1.5 GB of free RAM.

Re-measure before raising `WORKERS` or `MAX_LINKS`:

```bash
docker stats --no-stream --format '{{.MemUsage}}' <container>
```

An out-of-memory kill looks like a job dying with no error and the container
restarting — not like an exception.

---

## 11. Do not poll for status on an auto-scaling host

> Not a concern on a single server — one instance, one database. It matters the
> moment you run more than one replica.

Every instance has its own SQLite. A status poll can land on an instance that
has never heard of the job and answer `404` while the capture runs perfectly
somewhere else.

`/run-inline` therefore streams **NDJSON — one full job status per line**, the
same shape as the status endpoint, and the page renders from that stream. The
streaming response is pinned to the instance doing the work, so it is the only
status source that is always right.

---

## 12. Concurrency is bounded by RAM — and by CORES

Worst-case simultaneous browsers = `MAX_CONCURRENT_JOBS × MAX_WORKERS` — not
`× WORKERS`, because the form's per-job picker can ask for more than the default
— and each browser is ~0.5–1 GB. Overshoot and the kernel kills a job mid-run.

**Cores bound the speed-up; RAM bounds the crash.** Capture is CPU-heavy — each
worker decodes that post's images and video — so browsers only run in parallel
if there are cores to run them on. On a 1-vCPU box, three workers take turns on
one core: the wall-clock barely moves while the memory cost is real and
immediate. "It finished in minutes" comes from adding vCPUs, not from raising
`WORKERS` on the same box.

The form's **Capture speed** picker is per job and clamped to `MAX_WORKERS` in
two places — `routes_jobs.submit_job` and `build_command` — because the second
is also reachable from a stored job record, and the failure mode of one browser
too many is an OOM kill rather than a validation error. Set `MAX_WORKERS` to
roughly the vCPU count.

* The **Influencer report uses one worker** (`INFLUENCER_WORKERS=1`). Its
  follower-count cache lives in the worker *process*, so a second worker
  re-fetches the same profiles. Raise it only if your lists rarely repeat an
  account. The form's Capture speed picker deliberately does **not** apply here
  — it is hidden for this report type and ignored in `build_command` even if a
  crafted POST supplies it, so this invariant holds no matter what the UI does.
* `shm_size: "1gb"` in `docker-compose.yml` is not optional. Docker's default
  64 MB of shared memory makes Chromium crash on media-heavy posts.
* **Bind mounts inherit HOST ownership, not the image's.** The Dockerfile
  chowns `/app/data` to UID 1000, but mounting a root-owned host folder over it
  wins. Deploying as root without `chown -R 1000:1000 data sessions reports`
  gives `PermissionError: /app/data/jobs` at startup, a crash-loop, and a 502
  from the proxy — which looks like a networking fault and is not one.

---

## 13. Secrets

* `.env` and `sessions/x_state.json` are gitignored, absent from the image, and
  never served. Check `git ls-files` after any restructure.
* Use a **dedicated, throwaway X account**. Bulk captures get accounts
  rate-limited and suspended.
* `sessions/x_state.json` is a live login — whoever holds it can act as the
  capture account. Keep it on the server only; never commit it, never put it in
  a repo.
* **Never delete a hand-uploaded cookie.** `invalidate()` only removes the
  session when credentials exist to recreate it; otherwise the admin is left
  with nothing and no way back.
* `printf`, not `echo`, when piping a secret — a trailing newline becomes part
  of the password and the login fails in a way that looks like a wrong password.

---

## 14. Document-building gotchas

**DOCX**

* Nested tables are unreliable: under a fixed layout Word takes column widths
  from `w:tblGrid`, which `python-docx` does **not** update when you set
  `cell.width`. Prefer **paragraph borders + a right tab stop** — every renderer
  lays those out the same way.
* A right tab stop must sit **inside** the cell's text area (cell width less
  Word's 0.08 in side margins) or it is silently discarded and the value lands
  mid-line.
* `cell.add_table()` appends an empty paragraph after the table. Reuse it
  instead of adding your own, or you get stray blank lines.

**PDF (reportlab)**

* Use `VALIGN BOTTOM`, not `MIDDLE`, when a row mixes font sizes — middle
  alignment leaves the larger text visibly sitting below its label.
* Register a Unicode TTF or non-Latin titles render as black boxes.

**Both**

* macOS Quick Look is not a renderer. It ignores nested-table widths and
  substitutes fonts. Verify a DOCX in Word/WPS, or by inspecting the XML.

---

## 15. Front-end gotcha that will waste an hour

An author `display` rule beats the `hidden` attribute. Without this, everything
you mark `hidden` still renders:

```css
[hidden] { display: none !important; }
```

---

## 16. Playwright habits

* `locator(sel).first` can resolve to a **hidden** element and time out while a
  visible one sits right there. Use `.locator("visible=true").first`.
* Comma-separated selectors resolve as one set — if the alternatives differ in
  visibility, try them one at a time.
* Element screenshots do not save you from a broken page layout: if something is
  painted *over* your element, it is in the pixels either way.

---

## 17. Third-party APIs are eventually consistent

GitHub's Contents API can return 404 for a file you just wrote. The store's
save→read round-trip failed once for exactly this reason and returned `False`
**silently**.

**Rule: log every failure branch.** A silent `return False` in a cold-start path
is indistinguishable from a misconfiguration, and you will debug the wrong thing.

---

## 18. When adding a third report type

The Influencer report is the template for how to do this without touching frozen
code:

1. New folder, parallel to `src/` — capture, runner, worker, builder, entrypoint.
2. Its worker imports its own capture **directly**, so the frozen dispatcher and
   `src/_worker.py` need no routing change.
3. It may import `shot_quality`, `input_loader`, `platforms` and
   `browser_backend` read-only.
4. Add it to `REPORT_TYPES` in `config.py` and to `build_command()` in
   `webapp/jobs/runner.py`.
5. Mirror the proven structure (retry pass, quality pass, `results.json`) rather
   than inventing a new one.

---

## 18a. Styles designed in the app are profiles, and only profiles

The style designer (`webapp/styles.py`, `/styles`) writes profile JSON to
`data/profiles/`. Three things keep it inside the lines drawn by rule 1 and the
design note:

* Every save and every preview goes through `registry.resolve()` — the same
  merge + `validate()` a file in `profiles/registry/` gets. A style the runner
  would refuse cannot be saved, and the error shown is the registry's own.
* Built-in and shipped slugs are reserved (`styles.reserved_slugs()`), and the
  shipped registry is consulted first, so a user style can never shadow one.
* The runner copies the chosen user style into the job's private
  `profiles/registry/` (`runner._copy_user_profiles`), so the subprocess reads
  it through the normal path and a later edit or delete cannot change a job
  already running.

The designer can only set what the schema already allows. A new *capture*
behaviour is still a new engine (rule 18), not a form field.

---

## 18b. The Facebook engine is a profile engine, and starts logged-out

`facebook/fb_capture.py` is rule 18 done literally: its own folder, imported
directly by `profiles/prof_worker.py`, no change to `src/` or `influencer/`.
It is reached ONLY through `profiles/run_profile.py --profile <fb style>`;
there is no `run_facebook.py`, and `run.py` never sees a Facebook link.

Three facts to keep straight:

* **Links are platform-scoped at the edge.** `profiles/netlinks.py` decides
  what a Facebook link is, for BOTH the preview (`uploads.analyse(..., platform)`)
  and the job (`prof_runner` → `netlinks.load_rows`). The frozen
  `input_loader._rows_from_grid` stays the X reader; its layout helpers are
  imported read-only, its X-only filter is not copied. A profile's `platform`
  must match its engine's platform (`registry.validate` refuses otherwise).
* **No account is the normal state.** Public Page posts render for a
  logged-out desktop visitor once the login sheet, backdrop, cookie banner and
  bottom bar are removed and the scroll-lock released — the same one-bug logic
  as rule 19, re-implemented for Facebook's layers. `sessions/fb_state.json`
  is honoured if present and never required; `health.py` reports "no account
  needed" as OK, not as a warning.
* **The frame ends at the actions row.** Like · Comment · Share is the one
  boundary (rule 6.2 again): above it when `keep_engagement` is false, below it
  when true. Comments are `role="article"` nodes *inside* the post's article,
  so cutting at that row is also what keeps them out. If the row is not found
  the whole article ships with `frame_ok=False`.

Selectors are Facebook's Aug 2026 desktop DOM. When it breaks, run
`scripts/probe_logged_out.py` on the link and look (rule 3) before touching
the engine.

---

## 18c. Web-layer rules (v2 dashboard) — short, each one earned

1. **Assets are versioned.** `base.html` links `app.css?v=<hash>` /
   `app.js?v=<hash>` (`main._asset_version`). The first deploy of v2 rendered
   as an unstyled list on a colleague's Chrome because it paired the OLD
   cached stylesheet with the new markup. Never link a static asset bare.
2. **Hiding is not a gate.** Anything admin-only is hidden in the template
   AND refused by `auth.require_admin` (pages) / the same dependency on the
   write APIs (`/api/styles` save/preview/delete). Same principle as the
   platform pills: a disabled button is a hint, `check_runnable` is the gate.
3. **The dashboard shows report information only.** Health, budget, sessions
   and settings live under *Admin* in the nav. A colleague who only makes
   reports must be able to use the app without ever seeing them; the one
   exception is a plain "X capture is currently unavailable — ask an admin"
   line, because that changes whether their run will work.
4. **A thumbnail never pretends to be legible.** Style rows carry a 44 px
   thumbnail for recognition; the *Sample* modal shows the readable page. Big
   cards with unreadable content were tried and rejected by the people using it.
5. **Preview and submit share one parser.** `_grid_from_request` +
   `uploads.analyse(grid, dedupe, platform)` for both. If a link shows in the
   preview it is captured; if it is rejected it says which row and why.
6. **Platform is a form field, not a guess.** The preview re-reads when the
   platform pill changes; `netlinks.MATCHERS[platform]` decides what a link is
   in the web layer and in the runner. Never infer the platform from the URL
   mix.
7. **`.env` is read once.** `APP_USERS`/`APP_ADMINS`/everything else need a
   uvicorn restart; `--reload` watches `.py`, not `.env`. Usernames in
   `APP_ADMINS` must match `APP_USERS` exactly; passwords cannot contain `,`
   or `:`.
8. **Template styles are still profiles.** A Canva page becomes
   `template: {pages, slots, text}` on an ordinary profile; the registry
   validates it, `layout.placements` fits (never crops) into slots, and the
   engine that captures is chosen by `extends`. DOCX for these is labelled
   approximate — never promise pixel parity in Word.
9. **Users are data, not config.** Roles come from the `users` table via
   `auth.role_of`; `.env` is bootstrap only. Anything a role unlocks is
   guarded server-side (`require_admin` / `require_designer`).
10. **A blueprint exists for a reason.** `docs/BLUEPRINT.md` is the single
   file to hand to the next redesign; update it in the same commit as any
   structural change (new folder, new route family, new contract).

---

## 19. A stray dialog and a cropped reply are the same bug

Both captures used to clear overlays like this, once, right after load:

```python
for sel in ('[data-testid="BottomBar"]', '[role="dialog"] [aria-label="Close"]'):
    page.evaluate("(s)=>{const e=document.querySelector(s); if(e) e.remove()}", sel)
```

Read the second selector again: that removes the dialog's **close button** and
leaves the dialog. And it ran too early — X's "Confirm age in X mobile app"
sheet only opens *after* `_reveal_sensitive` clicks "View", which happens later.

The visible symptom was a QR code in the middle of a report. The invisible one
was worse:

> **While a modal is open, X locks the page (`overflow:hidden` /
> `position:fixed` on `<body>`) and `window.scrollBy` becomes a silent no-op.**

`_align_top` scrolls a parent tweet to the viewport edge with exactly that call.
Under the lock it does nothing, the clip starts at the wrong Y, and the reply is
sheared off the bottom of the frame — which reads as "the reply-capture code is
broken" and is nothing of the sort. One bug, two unrelated-looking symptoms.

`src/overlays.py` now owns all of it: click the real close button, then remove
`[role="dialog"]` / `sheetDialog` / `mask` / `BottomBar` and any fixed layer
covering ≥85% of the viewport from its top-left corner, then **release the
scroll lock whether or not anything was removed**. It runs on load, again after
the sensitive-content gate, and again before every retake — X re-renders the
column as media settles and brings sheets back.

Two things it deliberately does *not* do:

* **Never remove a fixed element that contains an `article`,** and never one
  narrower than 85% of the viewport. X's left nav rail is fixed and 275 px wide;
  taking it out reflows the column mid-capture (rule 4 territory).
* **Never keep clicking an age gate.** Since July 2026 X can demand age
  verification through its mobile app, which a desktop browser cannot satisfy at
  all. Those posts return `status="age_restricted"` and are reported, not
  screenshotted as a grey placeholder.

Also note `article_age_gated` subtracts the post's own `tweetText` before
matching. Without that, a tweet *about* age-restricted content gets dropped as
if it were one.

---

## 20. A guard gated on rendered text will decline to act on its own target

The Twitter report's defining promise is that a reply is shot together with the
post it answers (approved edit 1). It was breaking **silently**, on the live
tool, before any of the DPR work — and every gate said the run was perfect.

**The symptom.** A reply captured alone: no parent, no context. `status="ok"`,
`frame_ok=True`, `overlay=False`, `[verify] 60/60 links produced a clean
screenshot`. Nothing in the log, nothing in the UI, nothing in the document to
say a post had lost half its meaning. Rule 3 again, and it cost a day: the only
way this was ever going to be found was by *opening the pictures*.

**The mechanism**, in order:

1. `_locate_focused` calls `scroll_into_view_if_needed` — it must, because X
   unmounts off-screen articles (rule 6.1).
2. That scroll pushes the **parent** off-screen, and X virtualises it away.
3. The reply is now the only article, so its index is 0.
4. `first = max(0, idx - 1)` is also 0, so `top_el` is `None`.
5. `_frame_covers`' parent check is guarded by `if top_el is not None`, so it
   never runs. A one-article frame is validated against a one-article promise
   and passes.

**Why the existing guard did not save us.** `_ensure_parent` was written for
exactly this failure. It bailed on:

```python
if idx > 0 or not _is_reply(tweet):      # <- the hole
```

and `_is_reply` tests for the literal string `"Replying to"`. **X omits that
line whenever the parent is rendered directly above in conversation view** —
which is the only situation where step 2 above can happen. So the guard's
precondition was false in precisely the case it existed to catch.

**The evidence** (two instrumented 60-link runs, DPR 1, an all-reply set):

| | parent lost |
|---|---|
| healthy session | 4/60 (6.7%) |
| degraded session (X throttling) | 38/60 (63.3%) |
| with ~2s extra settle per capture | **0/60, twice** |

**42 of 42 losses took the `bailed_not_reply` branch. Not one reached the
remount** — so the single 600 ms attempt was never the bottleneck; control
simply never got there. Do not "fix" a guard's retry budget before checking
whether its precondition is ever true.

**The fix** trades rendered text for a structural fact: `_locate_focused` now
records `idx_before`, the article index from *before* it scrolls, which is the
only moment a virtualised ancestor is still mounted. Only a post that HAD an
ancestor can have lost one. A genuine root post has `idx_before == 0`, takes the
same early return it always did, and pays nothing — no extra scroll, no extra
wait.

**Demotion is allowed here, and that is a deliberate exception to rule 7.**
The pixel checks never demote because they infer. This one *observes*: the
capture saw an ancestor, and the frame it produced has one article. A reply
printed without the post it answers is **wrong** evidence, and wrong evidence is
worse than missing evidence with an explanation — so after every retake is
spent, the link is demoted to `status="parent_lost"` and listed as not-included
with a plain reason. `report_builder._usable()` drops it with no builder change.

**The fallback lever, if the index-based trigger ever proves flaky:** simply
giving the page more time made the bug vanish entirely (0/120 above). A settle
delay before the frame is decided is the blunt instrument that works; it costs
wall-clock on every capture, which is why it is the fallback and not the fix.

**Not affected:** `influencer/inf_capture.py` has no ancestor logic at all — it
frames a single post by design — so there is no parallel bug there.

---

## 21. Capture budget: ~320 posts a day and the session starts to rot

Measured on 2026-08-03, one shared account, ~320 captures inside a few hours.
The tail of that day looked like a different tool:

| | early runs | after ~320 captures |
|---|---|---|
| links needing a retry pass | 0 | 18 / 60 |
| low-quality recaptures | 1 | 8 / 60 |
| smallest frame produced | 598x152 | **598x80** |
| parent losses (rule 20) | 4 / 60 | 38 / 60 |

Nothing errored. X simply served slower, thinner pages, and every timing-sensitive
part of the capture degraded together. Rule 13 says bulk captures get accounts
rate-limited; this is what that looks like *before* the suspension.

**Consequences:**

* **Benchmark numbers from a throttled session prove nothing.** If a run shows
  an unusual retry count, throw the numbers away and re-run after a rest — do
  not average a healthy run with a degraded one.
* **~320/day is the working ceiling** until someone measures a better one. When
  scheduling or batch generation is built, that number is the budget, not
  `MAX_LINKS`.
* A second capture account buys headroom more cheaply than any code change.

---

## Checklist before you ship a change

- [ ] `git status` on `run.py`, `src/`, `install.py`, `requirements.txt` is clean
- [ ] Ran **both** report types end to end
- [ ] **Opened the PDF and the DOCX and looked at them**
- [ ] Tested with a reply URL, a video post and an age-restricted post
- [ ] Confirmed every reply screenshot still shows its **parent post** (rule 20)
- [ ] Confirmed the run itself was healthy — an unusual retry/recapture count
      means a throttled session, and its numbers prove nothing (rule 21)
- [ ] Confirmed no X dialog, action bar or "Hide" toggle is in any screenshot
- [ ] Checked peak memory if anything touches document building
- [ ] Confirmed `.env` / `sessions/` are still untracked
- [ ] Tested on the server, not only locally
- [ ] Signed in once as a NON-admin and confirmed the app is usable and shows nothing it should not
- [ ] `docs/BLUEPRINT.md` still describes the tree (rule 18c.8)

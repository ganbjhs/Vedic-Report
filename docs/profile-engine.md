# Profile engine — design note (for approval, not yet implemented)

Status: **draft for review.** Nothing here is built. It answers the brief's §2:
the profile JSON schema, the `shapes.py` API, how the profile runner reuses the
frozen capture read-only, and exactly how the two existing report types map onto
profiles.

Read [RULEBOOK.md](../RULEBOOK.md) rules 1, 2, 18, 20 and 21 first. This design
is shaped by all five.

---

## 1. What this is, and what it is not

**The claim being made:** report *presentation* — page size, N-up density, image
shape, framing, cover, outputs — is data, not code. A new presentation should be
a JSON file, not a new Python package.

**The claim NOT being made:** that a new *capture behaviour* is data. The
registry exposes a fixed set of capture knobs (engine, DPR, viewport, engagement
crop, thread depth). Anything outside that set is a new **engine**, and an engine
is code. It is worth being precise about this up front, because "adding a report
type is a JSON file" is true for the deck/contact-sheet/A5 family and false for
"capture the quote-tweet too".

So the honest payoff is: **the presentation axis becomes free; the capture axis
becomes cheap** (a new folder under rule 18 rather than a fork of `src/`).

### The unlock nobody should miss

`CTX_KWARGS` lives in `src/run_report.py`, which is frozen. `influencer/` has its
own copy, and `profiles/` would have a third. **A profile therefore owns its own
device scale factor**, which means the deferred DPR-2 decision stops being
all-or-nothing: a `high-res` profile can run at DPR 2 while the default Twitter
path stays exactly as it is today, with no edit to `src/` and no risk to the
report you depend on. That is a better outcome than flipping the global default,
and it is available for free once this exists.

---

## 2. Folder layout (rule 18, verbatim)

```
profiles/
  run_profile.py      entrypoint — mirrors run.py's CLI surface exactly
  prof_runner.py      owns CTX_KWARGS; parallel workers, retry pass, quality
                      pass, results.json — mirrors the proven shape
  prof_worker.py      one browser per process; imports the chosen engine DIRECTLY
  prof_builder.py     results.json + profile -> pdf / docx / html / xlsx
  shapes.py           the compositing layer (pure PIL, no I/O)
  registry.py         load + validate profile JSON, resolve inheritance
  registry/
    twitter.json          the existing Twitter report, expressed as a profile
    influencer.json       the existing Influencer report, expressed as a profile
    contact-sheet.json    NEW — 6-up visual index
    client-deck.json      NEW — padded 4:5, rounded, bordered, cover page
```

Rule 18 point 2 is why this works: `prof_worker` imports its engine directly, so
neither `src/capture/__init__.py`'s dispatcher nor `src/_worker.py` needs a
routing change.

---

## 3. Profile schema

One file per report type. Draft v1 — field names are the thing to argue about
now, because they become a compatibility surface the moment a profile is saved
in a preset.

```jsonc
{
  "schema": 1,
  "slug": "client-deck",              // unique; becomes the report_type
  "label": "Client Deck",             // shown in the UI
  "extends": "twitter",               // optional; shallow-merge over that profile
  "description": "Padded 4:5 cards with a cover page.",

  "capture": {
    "engine": "x",                    // "x" | "influencer" — see §4
    "device_scale_factor": 2,         // the unlock; 1 = today's behaviour
    "viewport": {"width": 1280, "height": 1600},
    "keep_engagement": true,
    "thread_ancestors": 1,            // see §4.2 — the one uncomfortable knob
    "workers": null                   // null = server default for this engine
  },

  "image": {
    "aspect": "4:5",                  // "W:H" | null = natural
    "fit": "pad",                     // "pad" | "fit" | "crop-top"
    "background": "#FFFFFF",
    "radius_px": 12,                  // at MASTER scale, see §5.3
    "border": {"px": 1, "color": "#E1E8ED"},
    "shadow": {"blur": 18, "opacity": 0.18, "dy": 6},
    "watermark": null
  },

  "page": {
    "size": "A4",                     // "letter" | "A4" | [w_in, h_in]
    "orientation": "portrait",
    "grid": [1, 2],                   // [cols, rows] -> per_page = 2
    "margins_in": [0.6, 0.6, 0.6, 0.6]
  },

  "content": {
    "cover": true,
    "header": "{title}",
    "footer": "{page} / {pages}",
    "per_post_fields": ["account_name", "post_link"],
    "metrics": null,                  // null, or an ordered label->key list
    "links_table": true
  },

  "outputs": ["pdf", "docx", "html", "xlsx"]
}
```

**Notes on the schema itself**

* `"schema": 1` is not ceremony. Presets (§6 of the brief) will store a profile
  slug, and profiles will drift. A version field is the cheapest possible
  migration lever and costs one integer now.
* `extends` keeps the new profiles honest: `client-deck` extending `twitter`
  means the diff *is* the design.
* Unknown keys are a **hard validation error**, not ignored. A typo'd
  `"radius-px"` that silently does nothing is exactly the class of bug rule 20
  is about.
* `grid` rather than `per_page` because the builder needs the shape anyway, and
  `per_page` is derivable. One source of truth.

---

## 4. How the profile runner reuses the frozen capture

### 4.1 Read-only import, no dispatcher change

`prof_worker.run_chunk` mirrors `src/_worker.run_chunk`'s pickled signature and
picks its engine by import:

```python
def run_chunk(chunk, headless, storage_state, ctx_kwargs, src_path, inf_path, engine):
    sys.path.insert(0, src_path)
    if engine == "influencer":
        sys.path.insert(0, inf_path)
        from inf_capture import capture as engine_capture
    else:
        from capture.x_capture import capture as engine_capture
    ...
```

Nothing under `src/` is written to. `shot_quality`, `input_loader`, `platforms`
and `overlays` are imported read-only, as rule 18 point 3 permits.

**Engine choice implies a result schema.** The `x` engine returns
`{status, handle, screenshot, text, overlay, frame_ok, parent_lost}`; the
`influencer` engine additionally returns `metrics`. A profile whose
`content.metrics` is non-null and whose `capture.engine` is `"x"` is a
validation error — catch it in `registry.py`, not at render time.

### 4.2 `thread_ancestors` — the one uncomfortable knob

`x_capture._THREAD_ANCESTORS` is a module constant read at call time
(`first = max(0, idx - _THREAD_ANCESTORS)`), so the brief is right that a
profile runner *can* set it before capture. Two ways to do it, and I want a
decision rather than a default:

**(a) Scoped monkey-patch, confined to `profiles/`.**

```python
@contextlib.contextmanager
def _thread_depth(engine_mod, depth):
    old = engine_mod._THREAD_ANCESTORS
    engine_mod._THREAD_ANCESTORS = depth
    try:
        yield
    finally:
        engine_mod._THREAD_ANCESTORS = old
```

Safe in practice: each worker is its own process, captures are sequential within
one, and the value is restored. But it is reaching into a frozen module's
private state, and it is invisible from `src/` — the exact shape of surprise
rule 1 exists to prevent.

**(b) Approved edit 6: `capture(..., thread_ancestors=None)`.** Additive,
defaulted, symmetric with `keep_engagement`'s precedent (approved edit 4), and
it makes the capability explicit where a reader of `src/` will find it.

**Recommendation: (b), asked for separately, and ship v1 without thread depth at
all** — none of the four launch profiles need it. Do not take a monkey-patch as
the price of a feature nothing has requested yet. If (a) is chosen anyway, it
must be a single documented call site with the context manager above, never a
bare assignment.

### 4.3 Stdout vocabulary is a hard contract

`webapp/jobs/runner.py::_Progress` parses the pipeline's stdout with literal
regexes. A profile runner that does not emit these **silently loses the progress
bar** — the UI shows a spinner and nothing else, and no test catches it. The
required lines, current as of approved edit 5:

| line | consumed by |
|---|---|
| `[input] skipped N non-X link(s)` | `_RE_SKIPPED` |
| `[runner] N X link(s) loaded` | `_RE_TOTAL` — also sets the progress total |
| `[runner] NO saved X session` | `_RE_NO_SESSION` |
| `[runner] capturing with N parallel worker(s)...` | `_RE_WORKERS` |
| `[runner] retrying N link(s) sequentially...` | `_RE_RETRY` |
| `[quality] recapturing N low-quality screenshot(s)...` | `_RE_QUALITY` |
| `[quality] dropping N shot(s) ...` | `_RE_BLOCKED` |
| `[quality] dropped N shot(s) whose parent ...` | `_RE_PARENT_LOST` |
| `[quality] N shot(s) may be missing ...` | `_RE_CROPPED` |
| `[verify] N/M links produced a clean screenshot` | `_RE_VERIFY` — drives "Building" |
| `  [x] <status> <handle> <account>` | `_RE_RESULT` |
| `[report] wrote <path>  (` | `_RE_WROTE` — drives "Packaging" |
| `[metrics] N post(s) had at least one metric` | `_RE_METRICS` (influencer only) |

**Proposal:** put these in `profiles/progress.py` as named emitter functions
(`emit_total(n)`, `emit_verify(good, total)`, …) shared by `prof_runner`, and add
a test that asserts each emitter's output matches the corresponding regex in
`webapp/jobs/runner.py` — importing the real regexes, so a change on either side
fails loudly. That converts an invisible coupling into a failing test.

### 4.4 Output location

`publish()` globs `reports/*.pdf` / `*.docx` by mtime, so `prof_builder` must
write into its own `ROOT/reports/` exactly as the other two builders do — where
`ROOT = Path(__file__).resolve().parents[1]`, which under rule 2's job isolation
resolves inside the job directory. HTML and XLSX are new artifact kinds and need
`_KINDS` in `webapp/routes_jobs.py` extended (additive; see §7).

---

## 5. `shapes.py`

### 5.1 Shape is a build-time concern — mostly

The brief's correction stands and this design assumes it: the master PNG is
**exactly the article's bounding box**, so you can never widen at build time.
Everything below therefore *adds* to the master rather than recovering anything
from it.

### 5.2 API

Pure functions, PIL in / PIL out, no file I/O, no globals:

```python
def compose(im: Image.Image, spec: dict) -> Image.Image:
    """Apply an image spec to one master screenshot. Order is fixed and
    deliberate: geometry, then decoration, then framing."""
```

Internally an ordered pipeline of named ops, each `(im, cfg) -> im`:

| op | cfg | notes |
|---|---|---|
| `fit_within` | `max_w`, `max_h` | ratio-preserving; the only downscale |
| `pad_to_aspect` | `aspect`, `background`, `anchor` | letterbox; never crops |
| `crop_to_aspect` | `aspect`, `anchor` | **lossy** — opt-in only |
| `rounded` | `radius_px` | alpha mask |
| `bordered` | `px`, `color` | drawn outside, grows the image |
| `shadowed` | `blur`, `opacity`, `dy` | grows the image; needs the canvas |
| `background` | `color` | flatten alpha last |

`compose` is deterministic and side-effect free, so it is **unit-testable
without a browser** — a real advantage over everything else in this codebase.

### 5.3 The scale trap, stated once

`radius_px`, `border.px`, `shadow.blur` are in **master pixels**. A master is
598 px wide at DPR 1 and 1196 px at DPR 2, so a 12 px radius is visually twice
as tight at DPR 2. Two options:

* **(a)** declare these in master px and document that a DPR-2 profile should
  double them — simple, surprising;
* **(b)** declare them in **points at final placement size** and have `compose`
  scale by `master_width / placement_width_pt` — correct, one more thing to get
  wrong.

**Recommendation: (b)**, because profiles are meant to be portable across DPR,
and (a) makes `device_scale_factor` a breaking change to every decorated
profile. Name the fields `radius_pt`, `border.pt`, `shadow.blur_pt` so the unit
is unmissable.

### 5.4 No new dependency

Pillow is already installed (it arrives transitively and `src/report_builder.py`
and `shot_quality.py` both use it). `requirements.txt` is inside the frozen set,
so if anything is ever needed it goes in `requirements-web.txt` — but nothing is.

---

## 6. Mapping the two existing report types

`registry/twitter.json`:

```jsonc
{ "schema": 1, "slug": "twitter", "label": "Twitter Report",
  "capture": {"engine": "x", "device_scale_factor": 1,
              "viewport": {"width": 1280, "height": 1600},
              "keep_engagement": false, "thread_ancestors": 1},
  "image":   {"aspect": null, "fit": "fit", "radius_pt": 0, "border": null},
  "page":    {"size": "letter", "orientation": "portrait", "grid": [1, 1],
              "margins_in": [0.75, 0.75, 0.75, 0.75]},
  "content": {"cover": false, "header": "{title}", "footer": null,
              "metrics": null, "links_table": true},
  "outputs": ["pdf", "docx"] }
```

`registry/influencer.json` differs in: `engine: "influencer"`, A4, `grid [1,2]`,
`margins 0.6`, and `content.metrics` listing Followers / Reactions / Comments /
Reach / Shares.

### 6.1 The acceptance test, and what "identical" can honestly mean

The brief asks for a diff against the current builders. Pixel-identical PDFs are
not a realistic bar — reportlab embeds timestamps, and rasterising would need a
new dependency. **What is achievable, and sufficient:**

1. **Geometry parity, asserted in code.** Feed the same `results.json` to the
   frozen builder and to `prof_builder` under `twitter.json`, and assert equal
   page count, and equal `(page_index, x_in, y_in, w_in, h_in)` for every image
   placement. This catches every layout regression and needs no rasteriser.
2. **Artifact shape.** Same file stems, same artifact kinds, same links-table
   row count and order.
3. **Eyes on it** (rule 3), once, on a real run — because a green assertion only
   proves the code did not raise, and that is exactly how rule 20's bug survived.

If (1) fails, **fix the abstraction, not the frozen builder** — the brief is
right and this is the load-bearing rule of the whole exercise.

**The two existing entrypoints stay live and default.** `run.py` and
`run_influencer.py` are untouched and remain what the `twitter` / `influencer`
report types invoke. The profile engine is selected explicitly, and only
*additional* profiles route through it, until geometry parity has been proven
twice and looked at once.

---

## 7. Web-layer changes (all outside `src/`)

The brief's §3 landmines, with the fix for each:

1. **`_CODE_ITEMS = ("run.py", "src", "influencer")`** in
   `webapp/jobs/runner.py` — add `"profiles"`, or the job dir has no engine and
   the subprocess dies with `can't open file`.
2. **`influencer = report_type != "twitter"`** (`webapp/jobs/runner.py:146`) —
   a binary where a table is needed. Add a third slug without fixing this and it
   **silently runs the influencer report**. Replace with a real descriptor,
   which also kills the other four hardcoded `== "twitter"` tests. Verified
   locations as of approved edit 5 — the brief's line numbers predate it:
   `routes_jobs.py:112`, `routes_jobs.py:124`, `runner.py:445`,
   `runner.py:452`:

   ```python
   # webapp/report_types.py — new, web-side, not frozen
   @dataclass(frozen=True)
   class ReportType:
       slug: str
       label: str
       argv: tuple            # e.g. ("run.py",) or ("profiles/run_profile.py", "--profile", "client-deck")
       worker_pool: str       # "capture" | "influencer"
       allows_worker_choice: bool
       allows_keep_engagement: bool
   ```

   The two capability flags are what the form and `build_command` should branch
   on. Today's behaviour falls out: `twitter` has both true, `influencer` both
   false — and the RULEBOOK rule 12 invariant (the influencer report ignores the
   worker picker even for a crafted POST) is then enforced by data.
3. **`config.REPORT_TYPES`** becomes built-ins + registry slugs. Because a
   profile slug *is* the `report_type`, the existing TEXT column stores it and
   **no new job column is needed** — which sidesteps the `public_job()` whitelist
   landmine entirely rather than working around it.
4. **`_KINDS`** in `routes_jobs.py` gains `html` and `xlsx` (additive dict
   entries + `publish()` copying them).

---

## 8. Risks

| risk | mitigation |
|---|---|
| Profile registry becomes a second, worse templating language | Hard cap: no logic in JSON — no conditionals, no expressions. If a profile needs an `if`, it needs an engine. |
| Every profile is a report type that can break, multiplying the acceptance surface | One cheap smoke profile-test: 2 links, assert artifacts exist and geometry validates. Runs per profile in CI-less form via one script. |
| Silent progress-bar loss from stdout drift | `profiles/progress.py` + the regex-parity test in §4.3 |
| Geometry parity proves layout but not appearance | Rule 3: look at one real run per profile before it ships |
| Capture-account budget (rule 21) | Profile smoke tests use 2 links; parity tests reuse a **stored** `results.json` and take no captures at all |

The last row matters more than it looks: **the whole parity test suite can run
against a checked-in fixture `results.json` with no browser**, which means the
expensive-and-rate-limited part of this project is not on the critical path for
most of this work.

---

## 9. Suggested phasing

1. `registry.py` + the two existing profiles + the **geometry parity test**
   (fixture-driven, zero captures). If parity fails, the abstraction is wrong —
   find out here, before anything depends on it.
2. `shapes.py` + its unit tests (zero captures).
3. `prof_runner` / `prof_worker` / `run_profile.py` + `progress.py` and the
   regex-parity test. First real capture run: 2 links.
4. `prof_builder` PDF, then DOCX, then HTML, then XLSX.
5. Web layer: `report_types.py`, `_CODE_ITEMS`, `_KINDS`, form wiring.
6. The two new profiles (`contact-sheet`, `client-deck`) — they exercise both
   axes of `shapes.py`.
7. A `high-res` profile at DPR 2, which is the cheap way to bank the resolution
   win without touching the default path.

Steps 1, 2 and most of 4 need **no browser at all**.

---

## 10. Open questions for you

1. **`thread_ancestors`** — approved edit 6 (§4.2b, recommended), scoped
   monkey-patch (§4.2a), or drop it from v1?
2. **Decoration units** — `radius_pt` at placement size (§5.3b, recommended) or
   `radius_px` at master size?
3. **Do the existing two types route through the engine once parity passes**, or
   stay on their own entrypoints permanently? I lean *stay*: they are proven,
   and the parity test gives the benefit without the risk.
4. **Is `xlsx` output per-profile or global?** It is a data export and barely
   presentational; it may belong outside the profile schema.
5. **Registry location** — in-repo JSON (versioned, reviewable) or
   `data/profiles/` (user-editable without a deploy)? I lean in-repo for v1;
   user-editable profiles need validation UI and a much stricter parser.

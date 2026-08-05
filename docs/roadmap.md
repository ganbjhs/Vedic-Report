# Roadmap

Two tracks. The infrastructure track is a means; the **input track is the
committed deliverable**. It is written down here because infrastructure work
expands to fill the time available, and these five features were agreed before
the profile engine existed and are not contingent on it.

---

## Track A — Input & repeatability (COMMITTED, not optional)

These ship. They are not "nice to have once the engine lands", and no
infrastructure phase may absorb their budget.

| # | Feature | Notes |
|---|---|---|
| A1 | **Pre-flight preview** — `POST /api/preview` | Parses an upload and returns the parsed rows **without creating a job**. Reuses `uploads.parse_rows` unchanged. Today the flow is upload-and-hope: you find out what will be captured only after spending browser minutes. Additive endpoint; no existing contract moves. |
| A2 | **Paste-links textarea** | An alternative to a file. Must auto-extract URLs from *messy* pasted text — arbitrary whitespace, junk words between links, trailing punctuation, duplicates. Reuses `input_loader`'s plain-list mode via `_rows_from_grid`, so the web layer and the CLI agree on what a paste means. `_clean_url` already strips trailing `.,;:!?)]}`. |
| A3 | **Google Sheets URL input** | **Published-CSV export only.** No OAuth, no private sheets. Strict host allow-list (`docs.google.com`), no redirects off it, connect+read timeouts, a response size cap, and an explicit content-type check — a permission failure returns an HTML login page, not CSV, and must be reported as "not published" rather than parsed as garbage. **Do not reuse `input_loader.is_x_url` as a URL validator**: it is a substring test, so `https://evil.com/?q=x.com` passes it. This is the project's first outbound HTTP; there is no existing fetch path or SSRF guard to inherit. |
| A4 | **Sheet/tab + column picker** | `_header_index` scans only the first 5 rows and silently takes the first matching column; `load_workbook` silently takes `wb.active`. When a workbook is ambiguous, ask instead of guessing. Pairs with A1 — you cannot pick a column you cannot see. |
| A5 | **Row-numbered rejection messages** | "row 14: `facebook.com/...` is not an X link" instead of a bare count. On a 200-row sheet the current message starts a manual hunt. |

**Ordering note.** A1 is a prerequisite in practice for A3 and A4: with a URL
input you have no idea what you are about to capture until you can see the
parsed rows, and a column picker needs somewhere to render. Build A1 first, then
A2 (cheapest, no new egress), then A3+A4 together, then A5 (touches
`uploads.parse_rows` messaging only).

**Constraint carried from the design note:** any new dependency goes in
`requirements-web.txt`. `requirements.txt` is inside the frozen set (rule 1).
Prefer stdlib `urllib.request` for A3 and avoid the dependency entirely.

---

## Track C — Post-merge, fresh branch (agreed 2026-08-05)

Both queued behind the merge. Neither needs captures.

### C1 — Auto-generated profile thumbnails on the dashboard cards

Replace the long description text on each profile card with a rendered mini page
showing that profile's *actual* geometry — 1-up letter, 2-up A4, the 2x3 contact
sheet, cover+card for the deck — drawn from the profile's own config through
`layout.placements()` and Pillow. Fixture screenshots or grey placeholder blocks,
whichever reads better at ~300px wide.

* Single-line caption under the image; the long description moves to a tooltip
  or an "info" affordance.
* Card click-through opens that profile's full sample PDF.
* Auto-regenerated, so a new or edited profile can never show a stale or missing
  image. Served as static files — no per-request rendering.

**Design points to settle when building:**
- *Freshness trigger.* Regenerating at app startup is simplest and guarantees
  correctness (four small PNGs, milliseconds). Alternative is a manifest keyed
  by a hash of each profile JSON, regenerating only on change — better if the
  profile count grows. Startup + hash-in-filename gives both freshness and cache
  busting.
- *Click-through needs a route.* `make_samples.py` writes to `data/samples/`,
  which is gitignored and outside `/static`. Serving it means a small
  authenticated route (`/samples/<slug>.pdf`) that resolves through a whitelist
  of known slugs — never a user-supplied path.
- Thumbnails belong in `webapp/static/profiles/` and should be gitignored, since
  they are derived artefacts.

### C2 — `influencer-deck`: a styled Influencer report

A new registry entry only. **`influencer.json` and the frozen
`inf_report_builder.py` are not touched**, and the existing parity tests must
stay green — that is the whole point of adding rather than editing.

Same A4, two posts per page, same five metric rows. Different presentation: each
post as a card like `client-deck` — padded 4:5, rounded corners, hairline
border, soft drop shadow — on a subtle off-white page so the cards lift.

**Known work beyond a JSON file** (this is a case where the registry alone is
not enough, exactly as the design note's scope section warns):
- `page.background` does not exist in the schema yet. Additive field + validation
  + honouring it in `prof_builder`'s three renderers.
- Metric rows currently draw as plain text lines. "Labelled chips or a neat
  table" is new drawing code in `prof_builder` (PDF, DOCX and HTML each).
- Influencer masters come from a 2000px-tall viewport and are typically much
  taller than the X ones, so padding them to 4:5 may letterbox heavily. Check
  against the fixture early and pick the aspect from what it actually looks
  like, rather than copying 4:5 from `client-deck` on principle.

**Approval gate:** generate a sample PDF from the stored fixture and iterate on
the look once before it ships.

---

## Track B — Profile engine (infrastructure)

See [profile-engine.md](profile-engine.md). Status:

| Phase | What | State |
|---|---|---|
| 1 | `registry.py`, `layout.py`, `shapes.py` + zero-capture tests | **done** |
| 2 | *(folded into 1)* | — |
| 3 | `progress.py`, `prof_runner`, `prof_worker`, `run_profile.py` | in progress |
| 4 | `prof_builder` — PDF, DOCX, HTML | in progress |
| 5 | Web layer: `report_types.py` dispatch table, `_CODE_ITEMS`, `_KINDS` | pending |
| 6 | New profiles: `contact-sheet`, `client-deck` | pending |
| 7 | `high-res` profile at DPR 2 (opt-in) — replaces the cancelled global default | pending |

---

## Blocked on the capture-account cooldown

Rule 21. Nothing here may start until the account has rested ~24h from
2026-08-03.

1. `scripts/acceptance/derive_roots.py` -> `roots.txt` -> `mixed20.txt` (~12 page loads)
2. `acceptance_parent_fix.py mixed --runs 2` — criterion: **zero silent parent losses**
3. `acceptance_parent_fix.py roots` — the untouched path is still untouched
4. `acceptance_parent_fix.py influencer` — unaffected end to end
5. Open the PDFs and the DOCX and look at them (rule 3)
6. Only then: merge `fix/parent-loss-approved-edit-5`
7. Only then: the `high-res` DPR 2 profile, re-benchmarked twice

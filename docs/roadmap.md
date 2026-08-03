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

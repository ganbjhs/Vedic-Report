# Engagement metadata

`metrics/x_metrics.py` visits X posts and comes back with **numbers, not
pixels**: likes, reposts, replies, views/reach and bookmarks, plus the author's
handle and the post's own timestamp.

    python metrics/x_metrics.py input.xlsx --json metrics.json --csv metrics.csv
    python metrics/x_metrics.py links.txt
    python metrics/x_metrics.py "https://x.com/<user>/status/<id>"

Input can be the canonical `input.xlsx` a job already has, a plain link list, or
the links themselves. Non-X links are ignored.

## What it reuses

Nothing here re-implements the parsing. `influencer/inf_capture.py` is imported
and its `_load_tweet`, `_pick_article`, `read_metrics` and `_read_handle` are
called as they stand — including the reply-page scoring that picks the post the
URL actually points at rather than the parent above it. A fix to the selectors
lands in both places at once. Bookmarks are the one thing added here, because
the influencer report never printed them.

Progress goes through `profiles/progress.py`, never a bare `print()`:
`webapp/jobs/runner.py` regex-matches these lines to drive the job page, and
`profiles/tests/test_progress_contract.py` holds both sides to it.

## In a report

Turn on **Read engagement numbers from X** in Project settings. Before the
capture starts, the job runs this reader over its own `input.xlsx` and fills in
the metric columns the sheet left blank — so a style that prints a metric
(`metric.like`, `metric.impressions`, `metric.views` in the deck templates)
prints a read number exactly as it prints a typed one. No builder changed.

Two rules it will not break:

* **A number typed into the sheet always wins.** The team reads theirs from
  Insights; this reads a public page. Overwriting a hand-checked figure would be
  the worst kind of helpful.
* **A number X did not show stays blank.** Never 0 — "nobody liked it" and "X
  did not tell us" are different facts.

Values are written in X's own compact form (984, 1.2K, 45K), which is how the
platform states them and what fits the metric pills. The exact integers are in
`metrics.json` and in the **Engagement (.csv)** download beside the report.

## Cost

One page load per link, sequential on purpose. This is a read, not a capture, so
parallel browsers would buy seconds while spending the shared X account's daily
budget faster — and that account, not CPU, is the scarce resource here
(RULEBOOK rule 21). Logged out, X shows no view count and often nothing at all,
so `sessions/x_state.json` is used when present.

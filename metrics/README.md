# Engagement metadata

Two readers, one setting. Both fill in **only the blank cells** of the sheet,
and neither ever writes a 0 for a number the platform did not show.

| | `x_metrics.py` — the page | `shot_metrics.py` — the screenshot |
|---|---|---|
| Reads | the live X post, logged in | the captured PNG, any platform |
| When | before the capture | after the capture, before the document |
| Needs | the shared X account | the `tesseract` binary |
| Gives | exact integers | the text as the picture prints it (`1.1K`, `3,275`) |
| For | X posts | Facebook, Instagram, and X posts the page would not open |

## `x_metrics.py` — numbers off the live page

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

## `shot_metrics.py` — numbers off the screenshot

    python metrics/shot_metrics.py shot.png
    python metrics/shot_metrics.py screenshots/ --platform facebook --csv read.csv
    python metrics/shot_metrics.py reports/results.json --fill

Facebook and Instagram never had a page route: a logged-out visitor gets the
login sheet and there is no shared account to sign in with (RULEBOOK §18b). But
every capture already **has the numbers in the picture** — "644 · 45 comments ·
11 shares" under a Facebook post, "1,234 likes" / "View all 56 comments" under
an Instagram one, the reply · repost · like · views bar under an X post. This
reader gets them back out of the pixels.

It reads what the picture shows: the **public** counts. Insights figures
(impressions and reach as the platform's own dashboard reports them) are not on
a public post and are not read — those columns stay whatever the team typed.

How it reads, in two passes through tesseract (the binary on PATH, called with
`subprocess`; no Python package):

1. **Labelled counts, whole image.** "3,275 Views", "45 comments", "11 shares",
   "1,234 likes", "View all 12 comments", "12.3K views", "2.1 lakh likes",
   Devanagari digits — any line that names its number, on any platform.
   Facebook's reactions count is the one number in the counts row with no word
   beside it. A doubtful number (Facebook's reaction glyphs sit against it and
   drag its confidence down) is cropped, cleaned of glyphs and read again.
2. **X's action bar.** Icons and numbers, no words. The row is found by its
   shape, the icons are erased (they are the tall blobs in that band), and the
   band is re-read at 4x with a digit-only vocabulary. Each number is assigned
   to a slot by **where it sits** — reply · repost · like · views/bookmark —
   because X prints nothing at all for a zero, so counting tokens left to right
   would shift every number after a missing one.

A screenshot with two posts (a reply shot with its parent) has two bars; the
**lower** one is read — the reply, the post the link points at, is underneath.

**Where a count goes is the style's decision.** A profile's top-level
`read_metrics` map names the sheet column(s) each read count fills — both
readers honour it (RULEBOOK §18d rule 6). Absent, the default is
`likes→like, comments→comments, shares→shares, views→views+reach+impressions`.
The **Kashi deck** says `{"likes": ["like"], "views": ["reach"]}`: a read post
fills its **Likes** and **Post Reach** pills and nothing else — Video views,
Impressions and ReTweets stay whatever the sheet typed. On the CLI:
`--map 'likes=like;views=reach'`.

The map may add **`"missing": {"views": "hidden"}`** — the word to print where
the post shows no such count at all (a Facebook photo post has no public view
count). Written only by this pass, only into a cell still blank, and never when
OCR could not run. The Kashi deck's Post Reach pill reads **hidden** in that
case. CLI: `--missing 'views=hidden'`.

What is written into the sheet is the text **as shown** (`1.1K`, not `1156`),
because that is what the picture proves. The parsed integers, the shown text
and a per-value *evidence* string (which line or which slot it came from) are
in `metrics_read.json` / `.csv` beside `results.json`, and in the report's
**Engagement (.csv)** download under *Source = Screenshot*.

Accuracy, measured: on the 16 X captures under `data/acceptance/influencer-1/`
(whose `results.json` holds the numbers the DOM reader saw at capture time) it
reads back **64 of 64** values. Facebook and Instagram are verified on rendered
mock-ups of their rows only — there are no such captures in the repo yet, so
**look at the first real run's CSV** (rule 3). `profiles/tests/test_shot_metrics.py`
holds all of it and needs no browser.

Optional second engine: `--engine grok` (or `SHOT_METRICS_ENGINE=grok`) sends
the picture to xAI's vision endpoint with `XAI_API_KEY` and asks for the same
fields as JSON. It is wired but has not been exercised against a live key; any
failure prints why and falls back to tesseract.

Install: the Docker image has it. Elsewhere `brew install tesseract` (Mac) or
`apt install tesseract-ocr tesseract-ocr-hin` (Ubuntu). Without it the job page
says so and the report prints what the sheet had — nothing else breaks.

## In a report

Turn on **Read engagement numbers from the posts** in Project settings. Two
things then happen, and a style that prints a metric (`metric.like`,
`metric.impressions`, `metric.views` in the deck templates) prints a read
number exactly as it prints a typed one. No builder changed.

* **Before the capture** (X and combined styles): `x_metrics.py` runs over the
  job's own `input.xlsx` and fills the metric columns the sheet left blank,
  one page load per X link.
* **After the capture** (every profile style — `profiles/run_profile.py
  --read-metrics`): `shot_metrics.py` runs over `results.json`, reads each
  screenshot whose row still has a blank cell, and fills those. In a combined
  report that means the Facebook and Instagram rows, plus any X row the page
  reader could not open; an X row already filled is not read again.

Two rules it will not break:

* **A number typed into the sheet always wins.** The team reads theirs from
  Insights; this reads a public page. Overwriting a hand-checked figure would be
  the worst kind of helpful.
* **A number X did not show stays blank.** Never 0 — "nobody liked it" and "X
  did not tell us" are different facts.

Values are written in the platform's own compact form (984, 1.2K, 45K), which is
how it states them and what fits the metric pills. The exact integers are in
`metrics.json` / `metrics_read.json` and in the **Engagement (.csv)** download
beside the report, with a *Source* column saying which reader saw what.

## Cost

One page load per link, sequential on purpose. This is a read, not a capture, so
parallel browsers would buy seconds while spending the shared X account's daily
budget faster — and that account, not CPU, is the scarce resource here
(RULEBOOK rule 21). Logged out, X shows no view count and often nothing at all,
so `sessions/x_state.json` is used when present.

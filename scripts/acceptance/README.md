# Acceptance sets for approved edit 5 (RULEBOOK rule 20)

Three link sets, each proving a different thing. Keep them **small** — rule 21:
the capture account degrades badly past roughly 320 posts a day, and a throttled
session makes every number here meaningless.

| file | what it is | proves |
|---|---|---|
| `replies.txt` | 20 URLs known to be replies (all of them were `truth_idx > 0` in the instrumented diagnosis) | the fix recovers the parent |
| `roots.txt` | root posts, no ancestor — **generated**, see below | the untouched path is genuinely untouched |
| `mixed20.txt` | ~20 links, ~8–10 replies + the rest roots | the realistic case |

## One-time: generate the root set

The repo's own link list is 100% replies, so root URLs have to be derived. The
parent of a reply *is* a root post:

```bash
.venv/bin/python scripts/acceptance/derive_roots.py replies.txt roots.txt --limit 12
```

That costs one page load per URL and takes no screenshots. Then build the mixed
set (10 replies + 10 roots):

```bash
cd scripts/acceptance
head -10 replies.txt  > mixed20.txt
head -10 roots.txt   >> mixed20.txt
wc -l mixed20.txt      # expect 20
```

> If `derive_roots.py` finds fewer than ~5 unique parents (these replies may all
> answer the same post), paste a few root-post URLs into `roots.txt` by hand.
> Any `x.com/<user>/status/<id>` that is not itself a reply will do.

## Running acceptance

```bash
.venv/bin/python scripts/acceptance_parent_fix.py mixed      --runs 2   # the criterion
.venv/bin/python scripts/acceptance_parent_fix.py roots      --runs 1   # no regression
.venv/bin/python scripts/acceptance_parent_fix.py influencer            # unaffected
.venv/bin/python scripts/acceptance_parent_fix.py dpr2       --runs 2   # only after the above pass
```

**Pass criterion:** `silent = 0` on every run. A `flagged` count above zero is
*not* a failure — that is the fix noticing a loss it could not recover, spending
its retakes and demoting the link with a stated reason. Wrong evidence is worse
than missing evidence with an explanation.

The harness refuses to report PASS when the session looks throttled (unusual
retry counts, or any frame under 180 px tall). If that happens, rest the account
and run it again — do not average a healthy run with a sick one.

**Then open the PDFs and the DOCX and look at them.** A green status only proves
the code did not raise (rule 3), and this whole bug shipped green for months.

Everything runs in throwaway copies under `data/acceptance/` (gitignored). The
repo's own `reports/` folder is never touched.

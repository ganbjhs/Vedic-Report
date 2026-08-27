# Facebook engine

`fb_capture.capture(page, url, shot_path, keep_engagement=True)` — frames one
public Facebook post for a **logged-out** browser (or a logged-in one, if
`sessions/fb_state.json` exists). Runs only through the profile engine:

    python profiles/run_profile.py links.xlsx --profile facebook --title "FB report"

Try a link first, without spending a report:

    python scripts/probe_logged_out.py "https://www.facebook.com/<page>/posts/<id>"

Selectors are Facebook's desktop DOM as of Aug 2026. When a capture looks
wrong, check in this order: (1) did the login dialog get removed (rule 19),
(2) is `div[role="article"]` still the post container, (3) is the Like button
still `aria-label="Like"` — that row is where the frame ends.

## Login walls (2026-08-27)

A public post that opens in a private window but comes back `login_wall` on
the server is Facebook **metering the logged-out visitor**, not a private
post: it hands out `datr` / `sb` cookies on the first visit and starts
redirecting permalinks to `/login` a few visits later from the same browser.
Three things now make every visit a first visit:

* `profiles/prof_worker.py` opens a **new browser context for every Facebook
  post** — no cookies, no storage, no HTTP cache from the previous link, and
  never the X login from a combined run (only a `sessions/fb_state.json`, if
  an admin put one there, is carried in). The context is closed after the
  shot.
* `fb_capture._reset_state` wipes every facebook.com cookie and the page's
  local/session storage before **each** navigation, so even a shared context
  (`scripts/capture_probe.py`) starts clean.
* `fb_capture.dismiss` closes the login sheet four ways before removing it:
  the labelled buttons, the sheet's **corner close control found by shape**
  (its label is "बंद करें" when the exit IP is Indian), Escape, then removal
  of the dialog, backdrop, bottom bar and banner with the scroll-lock released.

When the permalink still refuses, `fb_capture.capture` climbs a ladder:
permalink → permalink again from a clean slate → Facebook's **public post
plugin** (`/plugins/post.php?href=…`, the embed every website uses; videos try
the video plugin first, reels keep their /watch → video-plugin ladder). The
plugin renders public posts with no account and no dialogs — header, text,
media, counts — and the shot is framed from its card (`cut="plugin_card"`, or
`plugin_above_metrics` when the profile drops engagement). `res["via"]` names
the rung that rendered; `res["detail"]` lists what was tried when none did,
and the job's skipped list prints that instead of the X "session expired"
wording.

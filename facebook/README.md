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

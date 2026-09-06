#!/usr/bin/env python3
"""LOCAL ONLY — a demo client, a sign-in, and a month of sample posts for the
Client Portal, so the dashboard can be seen without Report Maker having
published anything yet.

    .venv/bin/python scripts/portal_dev_seed.py
    .venv/bin/python scripts/portal_dev_seed.py --email you@example.com --password 'at-least-10-chars'
    .venv/bin/python scripts/portal_dev_seed.py --remove          # take the demo client out again

Then sign in at http://127.0.0.1:8020/login with the e-mail and password it
prints. Safe to re-run: the client is upserted, the password is reset, the
sample rows are replaced.

What it writes to data/portal.db (and nothing else):
  * clients            one row, slug "demo", lag 2 days, categories mapped
  * client_users       one row with a real password hash (no invite needed)
  * post_metrics       ~30 sheet days x 4 categories x a few posts each,
                       the last two days hidden by the lag — the same shape
                       Report Maker's publish step produces
  * publish_log        one "backfill" line so Admin -> Clients shows it

It refuses to run when PORTAL_COOKIE_SECURE=1 (that is the server) unless
--force is given, and never touches any client other than "demo".
"""
import argparse
import datetime as _dt
import json
import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal import auth, config, schema, util          # noqa: E402

SLUG = "demo"
DEFAULT_EMAIL = "demo@example.com"
DEFAULT_PASSWORD = "Portal-Demo-1234"
DAYS = 30

# The sheet's own headings, as Report Maker publishes them, and how the demo
# client relabels them — the same mapping portal/tests uses.
CATEGORIES = [
    ("Hyper Local Pages Posting", "Hyper-local pages", ("facebook", "instagram"), 3),
    ("National X Influencers", "National influencers", ("x",), 3),
    ("3 Party Pages Posting", "3rd party pages", ("facebook", "instagram", "x"), 2),
    ("Counter Comments Links", "", ("x",), 2),                  # hidden from the client
]
CATEGORY_MAP = {
    "Hyper Local Pages Posting": {"label": "Hyper-local pages", "order": 1},
    "National X Influencers": {"label": "National influencers", "order": 2},
    "3 Party Pages Posting": {"label": "3rd party pages", "order": 3},
    "Counter Comments Links": {"label": "", "hidden": True},
}
HANDLES = {
    "facebook": [("kashi.updates", "Kashi Updates"), ("varanasi.today", "Varanasi Today"),
                 ("ganga.ghat.diaries", "Ganga Ghat Diaries"), ("purvanchal.news", "Purvanchal News")],
    "instagram": [("kashi_vibes", "Kashi Vibes"), ("banaras.walks", "Banaras Walks"),
                  ("up_stories", "UP Stories"), ("ghat.frames", "Ghat Frames")],
    "x": [("KashiLive", "Kashi Live"), ("UPVoices", "UP Voices"), ("BanarasBeat", "Banaras Beat"),
          ("GangaWatch", "Ganga Watch")],
}
CAPTIONS = [
    "Morning aarti at Dashashwamedh Ghat — the city wakes up in gold. #Varanasi",
    "New flyover stretch opened today; commute to the station is 15 minutes shorter.",
    "Weekend clean-up drive along Assi Ghat, 200+ volunteers turned up.",
    "Dev Deepawali prep has begun — lakhs of diyas being readied along the ghats.",
    "Traffic advisory for Godowlia this evening. Plan ahead.",
    "The weavers of Lallapura on how a Banarasi sari is born. Thread below.",
    "Metro survey teams spotted near Cantt station this week.",
    "Rain finally — 38 mm overnight, ghats partially submerged.",
    "Street food crawl: kachori-sabzi at 7 am is non-negotiable.",
    "Ropeway trial run from Cantt to Godowlia scheduled for next month.",
    "Sunset from Manikarnika, no filter.",
    "Civic body announces night sweeping on 42 main roads.",
]


def _post_url(platform: str, handle: str, n: int) -> str:
    if platform == "x":
        return f"https://x.com/{handle}/status/{1700000000000000000 + n}"
    if platform == "instagram":
        return f"https://www.instagram.com/{'reel' if n % 3 == 0 else 'p'}/DEMO{n:06d}/"
    return f"https://www.facebook.com/{handle}/posts/{900000000000 + n}"


def seed(conn, email: str, password: str, days: int) -> dict:
    now = time.time()
    today = util.today_in(config.PORTAL_TZ)
    lag = config.PORTAL_LAG_DAYS_DEFAULT

    row = conn.execute("SELECT id FROM clients WHERE slug = ?", (SLUG,)).fetchone()
    cid = row["id"] if row else uuid.uuid4().hex[:12]
    if row:
        conn.execute("UPDATE clients SET name = ?, display_name = ?, accent_hex = ?, lag_days = ?, tz = ?, "
                     "category_map = ?, show_screenshots = 0, archived = 0 WHERE id = ?",
                     ("Demo Client", "Demo Client", "#B8860B", lag, config.PORTAL_TZ,
                      json.dumps(CATEGORY_MAP), cid))
    else:
        conn.execute("INSERT INTO clients (id, slug, name, display_name, accent_hex, lag_days, tz, category_map, "
                     "show_screenshots, show_reports, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,0,0,?,?)",
                     (cid, SLUG, "Demo Client", "Demo Client", "#B8860B", lag, config.PORTAL_TZ,
                      json.dumps(CATEGORY_MAP), "portal_dev_seed", now))

    # The sign-in: a real password, no invite step.
    email = email.strip().lower()
    urow = conn.execute("SELECT id FROM client_users WHERE client_id = ? AND lower(email) = ?",
                        (cid, email)).fetchone()
    uid = urow["id"] if urow else uuid.uuid4().hex[:12]
    if urow:
        conn.execute("UPDATE client_users SET pw_hash = ?, role = 'manager', invite_token = '', "
                     "invite_expires = NULL, disabled = 0 WHERE id = ?", (auth.hash_password(password), uid))
    else:
        conn.execute("INSERT INTO client_users (id, client_id, email, pw_hash, role, invited_by, created_at) "
                     "VALUES (?, ?, ?, ?, 'manager', 'portal_dev_seed', ?)",
                     (uid, cid, email, auth.hash_password(password), now))
    conn.execute("DELETE FROM client_sessions WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM client_login_attempts")

    # Sample posts: deterministic, so re-running gives the same numbers.
    conn.execute("DELETE FROM post_metrics WHERE client_id = ?", (cid,))
    rnd = random.Random(20260906)
    n = 0
    for off in range(days - 1, -1, -1):
        day = today - _dt.timedelta(days=off)
        vis = day + _dt.timedelta(days=lag)
        weekend = day.weekday() >= 5
        for raw, _label, platforms, per_day in CATEGORIES:
            for _ in range(per_day + (1 if weekend else 0)):
                n += 1
                platform = rnd.choice(platforms)
                handle, name = rnd.choice(HANDLES[platform])
                url = _post_url(platform, handle, n)
                is_video = platform != "x" and n % 3 == 0
                base = {"facebook": 180, "instagram": 260, "x": 90}[platform]
                growth = 1.0 + (days - off) * 0.015           # a gentle upward trend
                likes = int(rnd.gauss(base, base * 0.35) * growth)
                likes = max(4, likes)
                comments = max(0, int(likes * rnd.uniform(0.04, 0.12)))
                shares = None if platform == "instagram" else max(0, int(likes * rnd.uniform(0.03, 0.10)))
                views = int(likes * rnd.uniform(9, 22)) if (is_video or platform == "x") else None
                posted = _dt.datetime.combine(day, _dt.time(rnd.randint(7, 21), rnd.randint(0, 59)))
                conn.execute(
                    "INSERT INTO post_metrics (client_id, project_id, run_id, sheet_date, visible_from, captured_at, "
                    "platform, category_raw, handle, display_name, post_url, post_url_norm, post_type, caption, "
                    "media_type, posted_at, collected_at, likes, comments, shares, views, metric_source, "
                    "raw_metrics, status, first_published_at, published_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, "demo-project", "demo-seed", util.day_str(day), util.day_str(vis), now,
                     platform, raw, "@" + handle, name, url, util.norm_url(url),
                     "reel" if is_video else "post", rnd.choice(CAPTIONS),
                     "video" if is_video else "image", posted.isoformat(timespec="minutes"),
                     (posted + _dt.timedelta(hours=lag * 24)).isoformat(timespec="minutes"),
                     likes, comments, shares, views, "scraper", "{}", "ok", now, now))
    conn.execute("INSERT INTO publish_log (client_id, run_id, kind, sheet_date, posts, visible_from, note, by_user, at) "
                 "VALUES (?, 'demo-seed', 'backfill', ?, ?, ?, 'portal_dev_seed sample data', 'portal_dev_seed', ?)",
                 (cid, util.day_str(today), n, util.day_str(today + _dt.timedelta(days=lag)), now))
    conn.commit()
    return {"client_id": cid, "user_id": uid, "email": email, "posts": n,
            "data_through": util.day_str(today - _dt.timedelta(days=lag))}


def remove(conn) -> int:
    row = conn.execute("SELECT id FROM clients WHERE slug = ?", (SLUG,)).fetchone()
    if not row:
        return 0
    cid = row["id"]
    for sql in ("DELETE FROM client_sessions WHERE client_id = ?",
                "DELETE FROM client_users WHERE client_id = ?",
                "DELETE FROM post_metrics WHERE client_id = ?",
                "DELETE FROM publish_log WHERE client_id = ?",
                "DELETE FROM client_projects WHERE client_id = ?",
                "DELETE FROM client_sources WHERE client_id = ?",
                "DELETE FROM scraper_cache WHERE client_id = ?",
                "DELETE FROM clients WHERE id = ?"):
        conn.execute(sql, (cid,))
    conn.commit()
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--days", type=int, default=DAYS, help=f"sheet days of sample data (default {DAYS})")
    ap.add_argument("--remove", action="store_true", help="delete the demo client, its user and its rows")
    ap.add_argument("--force", action="store_true", help="run even when PORTAL_COOKIE_SECURE=1")
    a = ap.parse_args()

    if config.COOKIE_SECURE and not a.force:
        print("PORTAL_COOKIE_SECURE=1 — this looks like the server. Refusing; pass --force if you mean it.")
        return 2
    if not a.remove:
        if not auth.EMAIL_RE.match(a.email):
            print("That is not an e-mail address.")
            return 2
        if len(a.password) < 10:
            print("Use a password of at least 10 characters (the invite form requires that too).")
            return 2

    schema.ensure_schema(config.PORTAL_DB)
    conn = schema.connect(config.PORTAL_DB)
    try:
        if a.remove:
            n = remove(conn)
            print("Demo client removed." if n else "No demo client to remove.")
            return 0
        r = seed(conn, a.email, a.password, a.days)
    finally:
        conn.close()

    print(f"Demo client ready in {config.PORTAL_DB}")
    print(f"  posts       {r['posts']} over {a.days} days (visible through {r['data_through']}; "
          f"the last {config.PORTAL_LAG_DAYS_DEFAULT} days are held back by the lag)")
    print(f"  sign in at  http://127.0.0.1:8020/login")
    print(f"  e-mail      {r['email']}")
    print(f"  password    {a.password}")
    print("Start the portal if it is not running:")
    print("  .venv/bin/python -m uvicorn portal.main:app --port 8020 --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())

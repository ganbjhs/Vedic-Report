#!/usr/bin/env python3
"""Zero-network, zero-browser tests for the Client Portal.

    .venv/bin/python portal/tests/test_portal.py

Covers: url normalisation and count parsing, the sealed API key, the scraper
adapter on a sample body, the publish step from a fake results.json, the
visibility rule (a row is invisible until sheet_date + lag), category
mapping, and — when FastAPI is installed — the HTTP layer end to end
(invite → login → /api/* → export, and that a second client's rows never
show up).
"""
import datetime as _dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="portal-test-")
os.environ["PORTAL_DB"] = str(Path(TMP) / "portal.db")
os.environ["PORTAL_MEDIA_DIR"] = str(Path(TMP) / "media")
os.environ["PORTAL_KEY_SECRET"] = "test-key-secret"
os.environ["PORTAL_SESSION_SECRET"] = "test-session-secret"

from portal import db, schema, scraper, secretbox, util  # noqa: E402


class TestUtil(unittest.TestCase):
    def test_norm_url(self):
        self.assertEqual(util.norm_url("https://twitter.com/a/status/1?s=20&t=abc"), "https://x.com/a/status/1")
        self.assertEqual(util.norm_url("www.instagram.com/reel/AbC/?igsh=xyz"), "https://instagram.com/reel/AbC")
        self.assertEqual(util.norm_url("https://m.facebook.com/reel/123/"), "https://facebook.com/reel/123")
        self.assertEqual(util.norm_url("https://www.facebook.com/photo.php?fbid=9&set=a.1&__cft__[0]=z"),
                         "https://facebook.com/photo.php?fbid=9&set=a.1")
        self.assertEqual(util.norm_url(""), "")

    def test_platform(self):
        self.assertEqual(util.platform_of("https://x.com/i/status/1"), "x")
        self.assertEqual(util.platform_of("https://www.instagram.com/p/1"), "instagram")
        self.assertEqual(util.platform_of("https://fb.watch/abc"), "facebook")
        self.assertEqual(util.platform_of("", "IG"), "instagram")

    def test_to_int(self):
        self.assertEqual(util.to_int("1.1K"), 1100)
        self.assertEqual(util.to_int("63,900"), 63900)
        self.assertEqual(util.to_int("2.1 lakh"), 210000)
        self.assertEqual(util.to_int("3,275 Views"), 3275)
        self.assertEqual(util.to_int("१२३"), 123)
        self.assertIsNone(util.to_int("hidden"))
        self.assertIsNone(util.to_int("—"))
        self.assertIsNone(util.to_int(""))
        self.assertIsNone(util.to_int(None))
        self.assertEqual(util.to_int(42.4), 42)

    def test_parse_day(self):
        self.assertEqual(util.parse_day("3/9/26"), _dt.date(2026, 9, 3))
        self.assertEqual(util.parse_day("2026-09-03"), _dt.date(2026, 9, 3))
        self.assertEqual(util.parse_day("04-09-2026"), _dt.date(2026, 9, 4))
        self.assertIsNone(util.parse_day("Tweet Links"))


class TestSecretbox(unittest.TestCase):
    def test_roundtrip(self):
        s = secretbox.seal("secret", "sk-live-abc")
        self.assertTrue(secretbox.is_sealed(s))
        self.assertEqual(secretbox.open_("secret", s), "sk-live-abc")
        with self.assertRaises(ValueError):
            secretbox.open_("other", s)
        with self.assertRaises(ValueError):
            secretbox.open_("secret", s[:-2] + "zz")


class TestScraper(unittest.TestCase):
    def test_normalize(self):
        body = {"posts": [{"platform": "instagram", "username": "bjpbhajanlal", "full_name": "bjpbhajanlal",
                           "caption": "भीलवाड़ा में रोड शो", "taken_at": "2026-09-04T09:12:00+05:30",
                           "like_count": "142", "comment_count": 4, "view_count": 0,
                           "media": [{"type": "video", "thumbnail_url": "https://cdn/x.jpg"}],
                           "url": "https://www.instagram.com/reel/AbC/?igsh=1", "classification": "Hyper Local Pages Posting"}]}
        rows = scraper.normalize_many(body)
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertEqual(p["platform"], "instagram")
        self.assertEqual(p["handle"], "@bjpbhajanlal")
        self.assertEqual(p["likes"], 142)
        self.assertEqual(p["comments"], 4)
        self.assertEqual(p["views"], 0)
        self.assertIsNone(p["shares"])
        self.assertEqual(p["media_type"], "video")
        self.assertEqual(p["day"], "2026-09-04")
        self.assertEqual(p["post_url_norm"], "https://instagram.com/reel/AbC")
        self.assertEqual(p["category_raw"], "Hyper Local Pages Posting")

    def test_build_url(self):
        self.assertEqual(scraper.build_url("https://s/api?from={from}&to={to}", "2026-09-01", "2026-09-04"),
                         "https://s/api?from=2026-09-01&to=2026-09-04")
        self.assertEqual(scraper.build_url("https://s/api/{date}", "2026-09-01", "2026-09-04"),
                         "https://s/api/2026-09-04")


def _seed(db_path):
    """Two clients, one with rows around today so the lag can be checked."""
    schema.ensure_schema(db_path)
    conn = schema.connect(db_path)
    now = time.time()
    today = util.today_in("Asia/Kolkata")
    for cid, slug in (("c1", "alpha"), ("c2", "beta")):
        conn.execute("INSERT INTO clients (id, slug, name, lag_days, created_at, category_map) VALUES (?,?,?,2,?,?)",
                     (cid, slug, slug.title(), now,
                      json.dumps({"Counter Comments Links": {"label": "", "hidden": True},
                                  "3 Party Pages Posting": {"label": "3rd Party Pages", "order": 1}})))
    i = 0
    for off in range(0, 6):                      # sheet days today-5 … today
        d = today - _dt.timedelta(days=off)
        for cat, plat in (("Hyper Local Pages Posting", "facebook"), ("National X Influencers", "x"),
                          ("3 Party Pages Posting", "instagram"), ("Counter Comments Links", "x")):
            for k in range(2):
                i += 1
                url = f"https://x.com/i/status/{i}" if plat == "x" else f"https://{plat}.com/p/{i}"
                conn.execute(
                    "INSERT INTO post_metrics (client_id, sheet_date, visible_from, platform, category_raw, handle, "
                    "display_name, post_url, post_url_norm, likes, comments, shares, views, metric_source, "
                    "first_published_at, published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("c1", util.day_str(d), util.day_str(d + _dt.timedelta(days=2)), plat, cat, f"@h{i}", f"H {i}",
                     url, util.norm_url(url), 100 + i, 5, None if plat == "instagram" else 3, 1000, "ocr", now, now))
    conn.execute("INSERT INTO post_metrics (client_id, sheet_date, visible_from, platform, category_raw, post_url, "
                 "post_url_norm, likes, first_published_at, published_at) VALUES ('c2', ?, ?, 'x', 'Other', "
                 "'https://x.com/i/status/999', 'https://x.com/i/status/999', 7, ?, ?)",
                 (util.day_str(today - _dt.timedelta(days=5)), util.day_str(today - _dt.timedelta(days=3)), now, now))
    conn.commit()
    conn.close()
    return today


TODAY = _seed(os.environ["PORTAL_DB"])      # once, before any suite runs
# The publish tests file a post three days back. Anchored to TODAY, not to a
# literal date: hardcoded days silently fall outside the seeded range once the
# calendar passes them, and then an unrelated test starts failing.
PUB_DAY = TODAY - _dt.timedelta(days=3)
PUB, PUB_VIS = util.day_str(PUB_DAY), util.day_str(PUB_DAY + _dt.timedelta(days=2))


class TestVisibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.today = TODAY
        from portal import db, queries
        cls.db, cls.queries = db, queries
        db.init()

    def test_lag_hides_recent_days(self):
        c = self.db.client_get("c1")
        lo, hi = self.db.data_bounds(c)
        self.assertEqual(hi, util.day_str(self.today - _dt.timedelta(days=2)))
        rows = self.db.visible_posts(c)
        self.assertTrue(all(r["visible_from"] <= util.day_str(self.today) for r in rows))
        self.assertTrue(all(r["client_id"] == "c1" for r in rows))
        d = self.queries.daily(c, util.day_str(self.today - _dt.timedelta(days=1)))
        self.assertEqual(d["posts"], [])            # yesterday is not published yet

    def test_categories_map(self):
        c = self.db.client_get("c1")
        cats = self.queries.visible_categories(c)
        labels = [x["label"] for x in cats]
        self.assertNotIn("Counter Comments Links", labels)      # hidden
        self.assertEqual(labels[0], "3rd Party Pages")           # order 1 + relabel
        d = self.queries.daily(c, util.day_str(self.today - _dt.timedelta(days=2)))
        self.assertTrue(all(p["category"] != "Counter Comments Links" for p in d["posts"]))
        self.assertEqual(len(d["posts"]), 6)                      # 8 minus the 2 hidden

    def test_trend_clamps_and_aggregates(self):
        c = self.db.client_get("c1")
        t = self.queries.trend(c, util.day_str(self.today - _dt.timedelta(days=30)), util.day_str(self.today))
        self.assertEqual(t["to"], util.day_str(self.today - _dt.timedelta(days=2)))
        self.assertEqual(t["from"], util.day_str(self.today - _dt.timedelta(days=5)))
        self.assertTrue(all(r["category"] != "Counter Comments Links" for r in t["rows"]))
        fb = [r for r in t["rows"] if r["platform"] == "facebook"]
        self.assertEqual(fb[0]["posts"], 2)
        self.assertEqual(fb[0]["engagement"], fb[0]["likes"] + fb[0]["comments"] + fb[0]["shares"])

    def test_other_client_isolated(self):
        c2 = self.db.client_get("c2")
        rows = self.db.visible_posts(c2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["likes"], 7)


class TestPublish(unittest.TestCase):
    """The publish step, with the internal store stubbed out."""

    def test_1_publish_rows(self):
        from webapp import portal_publish as pp
        conn = schema.connect(os.environ["PORTAL_DB"])
        client = pp.client_get(conn, "c1")
        job = {"id": "job1", "project_id": "p1", "title": "T", "created_at": time.time(), "finished_at": time.time(),
               "sheet_date": PUB}
        results = [
            {"status": "ok", "screenshot": "/nope/01.png", "post_link": "https://x.com/AbC/status/555?s=20",
             "platform": "x", "category": "National X Influencers", "handle": "AbC", "display_name": "A B C",
             "sheet_metrics": {"like": "1.2K", "views": "63,900"}},
            {"status": "login_wall", "screenshot": None, "post_link": "https://www.facebook.com/reel/77/",
             "platform": "facebook", "category": "Hyper Local Pages Posting", "handle": "", "account_name": "Page"},
        ]
        read = {"/nope/01.png": {"likes": 1180, "comments": 12, "shares": 40, "views": 63900, "engine": "tesseract"}}
        n = pp._publish_rows(conn, client, job, results, read, PUB_DAY)
        conn.commit()
        self.assertEqual(n, 2)
        r = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://x.com/AbC/status/555'").fetchone()
        self.assertEqual(r["likes"], 1200)           # the sheet's typed number wins
        self.assertEqual(r["comments"], 12)          # OCR fills the blank
        self.assertEqual(r["views"], 63900)
        self.assertEqual(r["visible_from"], PUB_VIS)
        self.assertEqual(r["handle"], "@AbC")
        self.assertEqual(r["metric_source"], "mixed")
        s = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://facebook.com/reel/77'").fetchone()
        self.assertEqual(s["status"], "skipped")
        self.assertIsNone(s["likes"])
        # second publish of the same run updates, never duplicates
        n2 = pp._publish_rows(conn, client, job, results, read, PUB_DAY)
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM post_metrics WHERE client_id='c1' AND run_id='job1'").fetchone()[0]
        self.assertEqual((n2, cnt), (2, 2))
        conn.close()

    def test_2_fold_scraper(self):
        from webapp import portal_publish as pp
        conn = schema.connect(os.environ["PORTAL_DB"])
        client = pp.client_get(conn, "c1")
        posts = scraper.normalize_many([
            {"platform": "x", "url": "https://twitter.com/AbC/status/555", "text": "hello", "created_at": PUB + "T10:00:00+05:30",
             "favorite_count": 1300, "retweet_count": 45, "reply_count": 13, "view_count": 70000},
            {"platform": "instagram", "url": "https://www.instagram.com/reel/NEW1/", "caption": "new", "taken_at": PUB + "T11:00:00+05:30",
             "like_count": 9, "comment_count": 1, "category": "Hyper Local Pages Posting"},
        ])
        matched, added, snapped = pp._fold_scraper(conn, client, {"signature_name": "t"}, posts, 2,
                                                   PUB_DAY - _dt.timedelta(days=2),
                                                   PUB_DAY + _dt.timedelta(days=1),
                                                   pull_day=util.day_str(TODAY))
        conn.commit()
        self.assertEqual((matched, added, snapped), (1, 1, 2))
        r = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://x.com/AbC/status/555'").fetchone()
        self.assertEqual(r["likes"], 1300)
        self.assertEqual(r["caption"], "hello")
        self.assertEqual(r["metric_source"], "scraper")
        n = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://instagram.com/reel/NEW1'").fetchone()
        self.assertEqual(n["sheet_date"], PUB)
        self.assertEqual(n["visible_from"], PUB_VIS)
        conn.close()


class TestScheduleClocks(unittest.TestCase):
    """The sheet and the scraper are on separate clocks. Briefly they were not,
    which silently dropped a Google Sheet from hourly reads to one a day."""

    def setUp(self):
        from webapp import portal_publish as pp
        self.pp = pp
        self._at, self._min, self._last = pp.SYNC_AT, pp.SYNC_MINUTES, pp._LAST_RUN_DAY

    def tearDown(self):
        self.pp.SYNC_AT, self.pp.SYNC_MINUTES = self._at, self._min
        self.pp._LAST_RUN_DAY = self._last

    def _at_ist(self, hh, mm, day=10):
        import datetime as d
        from zoneinfo import ZoneInfo
        return d.datetime(2026, 9, day, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()

    def test_the_sheet_stays_hourly_even_when_a_daily_time_is_set(self):
        self.pp.SYNC_AT, self.pp.SYNC_MINUTES = "03:30", 60
        hourly = [t for t in range(0, 3600 * 6, 60) if self.pp._interval_due(t)]
        self.assertEqual(len(hourly), 6)          # once an hour, regardless of SYNC_AT

    def test_the_scraper_fires_once_at_the_local_time(self):
        self.pp.SYNC_AT, self.pp._LAST_RUN_DAY = "03:30", ""
        self.assertFalse(self.pp._daily_due(self._at_ist(3, 29)))
        self.assertTrue(self.pp._daily_due(self._at_ist(3, 30)))
        self.assertFalse(self.pp._daily_due(self._at_ist(3, 31)))   # not twice
        self.assertFalse(self.pp._daily_due(self._at_ist(20, 0)))   # nor later the same day
        self.assertTrue(self.pp._daily_due(self._at_ist(3, 30, day=11)))   # the next day

    def test_a_restart_after_the_hour_still_catches_the_day(self):
        self.pp.SYNC_AT, self.pp._LAST_RUN_DAY = "03:30", ""
        self.assertTrue(self.pp._daily_due(self._at_ist(9, 15)))

    def test_no_daily_time_means_the_old_interval(self):
        self.pp.SYNC_AT, self.pp.SYNC_MINUTES = "", 60
        self.assertEqual(self.pp._daily_due(3600), self.pp._interval_due(3600))


class TestMigration(unittest.TestCase):
    """A v1 database — one that has been running in production — must survive
    ensure_schema() and keep its rows. The trap: an index over a column added
    later cannot sit in the DDL script, because CREATE TABLE IF NOT EXISTS is a
    no-op on a database that already has the table."""

    def test_a_v1_database_upgrades_in_place(self):
        import sqlite3
        path = str(Path(TMP) / "v1.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE clients (id TEXT PRIMARY KEY, slug TEXT, name TEXT, lag_days INTEGER,
                                  tz TEXT, category_map TEXT, created_at REAL, archived INTEGER DEFAULT 0);
            CREATE TABLE client_users (id TEXT PRIMARY KEY, client_id TEXT, email TEXT, pw_hash TEXT,
                                       role TEXT, invite_token TEXT, invite_expires REAL,
                                       created_at REAL, last_login_at REAL, disabled INTEGER DEFAULT 0);
            CREATE TABLE client_sources (id TEXT PRIMARY KEY, client_id TEXT, signature_name TEXT,
                                         base_url TEXT, api_key_enc TEXT, auth_style TEXT,
                                         enabled INTEGER, added_by TEXT, added_at REAL,
                                         last_ok_at REAL, last_error TEXT, last_count INTEGER);
            CREATE TABLE post_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
                                       sheet_date TEXT NOT NULL, visible_from TEXT NOT NULL,
                                       platform TEXT NOT NULL, category_raw TEXT DEFAULT '',
                                       post_url TEXT NOT NULL, post_url_norm TEXT NOT NULL,
                                       likes INTEGER, metric_source TEXT DEFAULT 'none',
                                       raw_metrics TEXT DEFAULT '{}', status TEXT DEFAULT 'ok',
                                       first_published_at REAL NOT NULL, published_at REAL NOT NULL,
                                       UNIQUE (client_id, post_url_norm, sheet_date));
            INSERT INTO post_metrics (client_id, sheet_date, visible_from, platform, post_url,
                                      post_url_norm, likes, first_published_at, published_at)
            VALUES ('old', '2026-08-01', '2026-08-03', 'x', 'https://x.com/a/status/1',
                    'https://x.com/a/status/1', 42, 0, 0);
        """)
        c.commit(); c.close()

        schema.ensure_schema(path)                       # must not raise
        schema.ensure_schema(path)                       # and must be repeatable

        c = sqlite3.connect(path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(post_metrics)")}
        for new in ("tweet_id", "lang", "quotes", "bookmarks", "author_followers",
                    "last_refresh_ms", "refresh_count"):
            self.assertIn(new, cols, new)
        self.assertIn("probe_url", {r[1] for r in c.execute("PRAGMA table_info(client_sources)")})
        self.assertIn("username", {r[1] for r in c.execute("PRAGMA table_info(client_users)")})
        idx = {r[1] for r in c.execute("PRAGMA index_list(post_metrics)")}
        self.assertIn("pm_client_tweet", idx)
        self.assertTrue([r[1] for r in c.execute("PRAGMA table_info(post_metric_days)")])
        self.assertEqual(c.execute("SELECT likes FROM post_metrics").fetchone()[0], 42)
        self.assertEqual(c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "2")
        c.close()


class TestCollector(unittest.TestCase):
    """The Collector contract: history is built, not overwritten; a status is
    obeyed; a platform we do not model is skipped rather than guessed at."""

    D1 = util.day_str(TODAY - _dt.timedelta(days=9))
    D2 = util.day_str(TODAY - _dt.timedelta(days=8))
    URL = "https://x.com/Rani/status/1888800000000000001"

    def setUp(self):
        from webapp import portal_publish as pp
        self.pp = pp
        self.conn = schema.connect(os.environ["PORTAL_DB"])
        self.conn.execute("INSERT OR IGNORE INTO clients (id, slug, name, lag_days, created_at) "
                          "VALUES ('c3','gamma','Gamma',2,?)", (time.time(),))
        self.conn.execute("DELETE FROM post_metrics WHERE client_id='c3'")
        self.conn.execute("DELETE FROM post_metric_days WHERE client_id='c3'")
        # the same post filed on two different sheet days
        for day, likes in ((self.D1, 100), (self.D2, 150)):
            self.conn.execute(
                "INSERT INTO post_metrics (client_id, sheet_date, visible_from, platform, category_raw, "
                "post_url, post_url_norm, likes, metric_source, first_published_at, published_at) "
                "VALUES ('c3',?,?,'x','National X Influencers',?,?,?,'sheet',?,?)",
                (day, util.day_str(util.parse_day(day) + _dt.timedelta(days=2)), self.URL,
                 util.norm_url(self.URL), likes, time.time(), time.time()))
        self.conn.commit()
        self.client = pp.client_get(self.conn, "c3")

    def tearDown(self):
        self.conn.close()

    def _fold(self, rows, pull_day):
        posts = scraper.normalize_many(rows)
        r = self.pp._fold_scraper(self.conn, self.client, {"signature_name": "collector"}, posts, 2,
                                  TODAY - _dt.timedelta(days=30), TODAY, pull_day=pull_day)
        self.conn.commit()
        return r

    def _row(self, day):
        return self.conn.execute("SELECT * FROM post_metrics WHERE client_id='c3' AND sheet_date=?",
                                 (day,)).fetchone()

    def test_only_the_posts_own_day_is_touched(self):
        """The bug that made every growth chart flat: one pull used to rewrite
        the post's numbers on EVERY day it had ever appeared."""
        matched, added, snapped = self._fold([{
            "platform": "x", "tweet_id": "1888800000000000001", "url": self.URL,
            "day": self.D2, "group": "National X Influencers", "status": "ok",
            "like_count": 999, "reply_count": 7, "retweet_count": 3, "view_count": 40000,
            "quote_count": 2, "bookmark_count": 11, "last_refresh_ms": 1789000000000,
        }], pull_day=util.day_str(TODAY))
        self.assertEqual((matched, added, snapped), (1, 0, 1))
        self.assertEqual(self._row(self.D2)["likes"], 999)       # this day is refreshed
        self.assertEqual(self._row(self.D1)["likes"], 100)       # yesterday is HISTORY, untouched
        self.assertEqual(self._row(self.D2)["quotes"], 2)
        self.assertEqual(self._row(self.D2)["bookmarks"], 11)
        self.assertEqual(self._row(self.D2)["tweet_id"], "1888800000000000001")

    def test_a_day_is_snapshotted_once_per_pull_day(self):
        row = {"platform": "x", "tweet_id": "1888800000000000001", "url": self.URL,
               "day": self.D2, "status": "ok", "like_count": 500}
        self._fold([row], pull_day="2026-09-01")
        self._fold([dict(row, like_count=520)], pull_day="2026-09-01")   # same day again
        self._fold([dict(row, like_count=610)], pull_day="2026-09-02")   # the next day
        got = self.conn.execute("SELECT day, likes FROM post_metric_days WHERE client_id='c3' "
                                "ORDER BY day").fetchall()
        self.assertEqual([(r["day"], r["likes"]) for r in got],
                         [("2026-09-01", 520), ("2026-09-02", 610)])

    def test_removed_and_unavailable_are_hidden(self):
        self._fold([{"platform": "x", "tweet_id": "1888800000000000001", "url": self.URL,
                     "day": self.D2, "status": "removed", "status_note": "gone from the sheet"}],
                   pull_day=util.day_str(TODAY))
        r = self._row(self.D2)
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(r["skip_reason"], "gone from the sheet")
        self.assertEqual(len(db.visible_posts(self.client, "AND sheet_date = ?", (self.D2,))), 0)

    def test_pending_is_shown_with_no_numbers(self):
        self._fold([{"platform": "x", "tweet_id": "2", "url": "https://x.com/u/status/2",
                     "day": self.D2, "status": "pending", "group": "National X Influencers"}],
                   pull_day=util.day_str(TODAY))
        r = self.conn.execute("SELECT * FROM post_metrics WHERE client_id='c3' AND tweet_id='2'").fetchone()
        self.assertEqual(r["status"], "ok")          # listed, not yet measured
        self.assertIsNone(r["likes"])                # never 0 for unknown

    def test_a_platform_we_do_not_model_is_skipped_not_guessed(self):
        """platform_of() ends `return "x"`, so a YouTube link would be charted
        as X. A row that states its platform is believed or dropped."""
        m, a, sn = self._fold([{"platform": "youtube", "url": "https://youtube.com/shorts/3V405Q",
                                "day": self.D2, "view_count": 5000}], pull_day=util.day_str(TODAY))
        self.assertEqual((m, a, sn), (0, 0, 0))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM post_metrics WHERE client_id='c3' AND platform='x' "
            "AND post_url LIKE '%youtube%'").fetchone()[0], 0)

    def test_tweet_id_survives_a_retyped_url(self):
        """The sheet is typed by hand and norm_url keeps path case, so the same
        post arrives as two URLs. The post id is the join key."""
        self._fold([{"platform": "x", "tweet_id": "1888800000000000001", "url": self.URL,
                     "day": self.D2, "like_count": 300}], pull_day=util.day_str(TODAY))
        m, a, _ = self._fold([{"platform": "x", "tweet_id": "1888800000000000001",
                               "url": "https://twitter.com/rani/status/1888800000000000001?s=20",
                               "day": self.D2, "like_count": 400}], pull_day=util.day_str(TODAY))
        self.assertEqual((m, a), (1, 0))             # matched, not duplicated
        self.assertEqual(self._row(self.D2)["likes"], 400)


class TestRealCollectorRow(unittest.TestCase):
    """The row scraper.vedictech.in actually serves, field names copied from a
    live /api/links?project=16 response on 10 Sep 2026. If the Collector ever
    renames one of these, this test fails instead of a count silently going
    missing from a client's dashboard."""

    ROW = {
        "platform": "x", "watchlist_id": 15, "watchlist": "6/9/26", "tab": "6/9/26",
        "day": "2026-09-06", "section": "National X Influencers", "group": "National X Influencers",
        "refresh_every_s": 86400, "sheet_row": 31, "added_via": "sheet",
        "url": "https://x.com/MeghUpdates/status/2096598745530122459",
        "post_url": "https://x.com/MeghUpdates/status/2096598745530122459",
        "tweet_id": "2096598745530122459",              # a string: > 2**53
        "status": "ok", "status_note": None,
        "added_at": "2026-09-10T11:03:38.507000+00:00",
        "last_refresh_ms": 1789040288000, "refresh_count": 1, "fail_streak": 0, "fetched": True,
        "created_at": "2026-09-06T13:57:48+00:00", "created_ms": 1788703068000,
        "text": "a post", "lang": "en",
        "author_username": "MeghUpdates", "author_display_name": "Megh Updates",
        "author_id": "11348282", "author_followers": 653326,
        "author_avatar": "https://pbs.twimg.com/profile/abc.jpg",
        "reply_count": 9, "retweet_count": 339, "like_count": 1193,
        "quote_count": 4, "view_count": 29985, "bookmark_count": 51,
        "is_retweet": False, "is_reply": False, "is_quote": False,
        "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/x.jpg",
                   "thumb": "https://pbs.twimg.com/media/x.jpg", "duration": None,
                   "thumbnail_url": "https://pbs.twimg.com/media/x.jpg"}],
        "collected_ms": 1789040288000, "last_seen_at": "2026-09-10T11:03:38+00:00",
    }

    def test_every_field_lands_somewhere(self):
        p = scraper.normalize(self.ROW)
        self.assertEqual(p["platform"], "x")
        self.assertEqual(p["post_id"], "2096598745530122459")
        self.assertEqual(p["day"], "2026-09-06")          # the tab's date, not the publish date
        self.assertTrue(p["day_given"])
        self.assertEqual(p["category_raw"], "National X Influencers")
        self.assertEqual(p["status"], "ok")
        self.assertEqual(p["handle"], "@MeghUpdates")
        self.assertEqual(p["display_name"], "Megh Updates")
        self.assertEqual(p["avatar_url"], "https://pbs.twimg.com/profile/abc.jpg")
        self.assertEqual(p["lang"], "en")
        self.assertEqual(p["author_followers"], 653326)
        self.assertEqual((p["likes"], p["shares"], p["comments"]), (1193, 339, 9))
        self.assertEqual((p["views"], p["quotes"], p["bookmarks"]), (29985, 4, 51))
        self.assertEqual(p["last_refresh_ms"], 1789040288000)
        self.assertEqual(p["refresh_count"], 1)
        self.assertEqual(p["media_type"], "image")
        self.assertEqual(p["thumb_url"], "https://pbs.twimg.com/media/x.jpg")
        self.assertEqual(p["posted_at"][:10], "2026-09-06")
        self.assertEqual(p["post_url_norm"], "https://x.com/MeghUpdates/status/2096598745530122459")

    def test_a_pending_row_carries_no_invented_zeros(self):
        raw = dict(self.ROW, status="pending", fetched=False, like_count=None, retweet_count=None,
                   reply_count=None, view_count=None, quote_count=None, bookmark_count=None)
        p = scraper.normalize(raw)
        self.assertEqual(p["status"], "pending")
        for k in ("likes", "shares", "comments", "views", "quotes", "bookmarks"):
            self.assertIsNone(p[k], k)

    def test_a_real_zero_survives(self):
        """0 quotes is a fact, not a missing value — it must not become None."""
        p = scraper.normalize(dict(self.ROW, quote_count=0, reply_count=0))
        self.assertEqual((p["quotes"], p["comments"]), (0, 0))

    def test_the_live_envelope(self):
        body = {"total": 1614, "limit": 500, "offset": 0,
                "rows": [self.ROW], "items": [self.ROW]}
        self.assertEqual(len(scraper.normalize_many(body)), 1)


class TestEnvelopeAndPaging(unittest.TestCase):
    def test_every_array_key_the_collector_might_use(self):
        row = {"platform": "x", "url": "https://x.com/u/status/9", "tweet_id": "9"}
        for key in ("items", "rows", "links", "posts", "data", "results", "records"):
            self.assertEqual(len(scraper.normalize_many({key: [row], "total": 1})), 1, key)
        self.assertEqual(len(scraper.normalize_many([row])), 1)

    def test_a_useless_envelope_says_what_it_got(self):
        with self.assertRaises(scraper.ScraperError) as e:
            scraper.normalize_many({"payload": [], "total": 0})
        self.assertIn("payload", str(e.exception))

    def test_a_stated_day_beats_the_posts_publish_time(self):
        p = scraper.normalize({"platform": "x", "url": "https://x.com/u/status/1",
                               "day": "2026-09-08", "created_at": "2026-07-21T10:00:00Z"})
        self.assertEqual((p["day"], p["day_given"]), ("2026-09-08", True))
        q = scraper.normalize({"platform": "x", "url": "https://x.com/u/status/1",
                               "created_at": "2026-07-21T10:00:00Z"})
        self.assertEqual((q["day"], q["day_given"]), ("2026-07-21", False))

    def test_paging_walks_the_whole_watchlist(self):
        """One GET used to import the first page and lose the rest, silently."""
        import urllib.parse as up
        made = [{"platform": "x", "tweet_id": str(i), "url": f"https://x.com/u/status/{i}",
                 "day": "2026-09-08", "like_count": i} for i in range(5)]
        seen = []

        def fake_get(url, api_key, auth_style="bearer", signature=""):
            q = dict(up.parse_qsl(up.urlsplit(url).query))
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 2))
            seen.append(off)
            return json.dumps({"total": 5, "limit": lim, "offset": off,
                               "rows": made[off:off + lim]}).encode()

        real, scraper._get = scraper._get, fake_get
        try:
            posts, _ = scraper.fetch({"base_url": "https://s/api/links?project=16&limit=2"},
                                     "k", "2026-09-01", "2026-09-08")
        finally:
            scraper._get = real
        self.assertEqual(seen, [0, 2, 4])
        self.assertEqual([p["post_id"] for p in posts], ["0", "1", "2", "3", "4"])

    def test_paging_stops_when_a_page_repeats_itself(self):
        row = {"platform": "x", "tweet_id": "7", "url": "https://x.com/u/status/7"}
        real = scraper._get
        scraper._get = lambda *a, **k: json.dumps({"total": 99, "rows": [row]}).encode()
        try:
            posts, _ = scraper.fetch({"base_url": "https://s/api/links"}, "k", "", "")
        finally:
            scraper._get = real
        self.assertEqual(len(posts), 1)


class TestHttp(unittest.TestCase):
    """End to end through the ASGI app — skipped when FastAPI is not installed."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            from portal.main import app
        except Exception as e:                                   # pragma: no cover
            raise unittest.SkipTest(f"fastapi not installed: {e}")
        # This flow drives the app directly, with no prefix-stripping proxy in
        # front, so it must run at the root. Neutralise any PORTAL_BASE_PATH from
        # the environment (e.g. the server's .env sets /portal) — otherwise the
        # session cookie is scoped to /portal and these root-path calls omit it.
        from portal import config as _cfg
        _cfg.BASE_PATH = ""
        cls.client = TestClient(app, base_url="http://portal.test")
        cls.client.__enter__()
        from portal import auth, db
        cls.auth, cls.db = auth, db
        made = auth.create_user(db.rw(), "c1", "person@example.com", "viewer", invited_by="test")
        cls.token = made["token"]

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_flow(self):
        c = self.client
        r = c.get("/api/meta")
        self.assertEqual(r.status_code, 401)
        r = c.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (303, 307))
        r = c.get(f"/invite/{self.token}")
        self.assertEqual(r.status_code, 200)
        r = c.post(f"/invite/{self.token}", data={"password": "correct horse battery", "confirm": "correct horse battery"},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("portal_sid", r.cookies)
        r = c.get("/api/meta")
        self.assertEqual(r.status_code, 200)
        m = r.json()
        self.assertEqual(m["lag_days"], 2)
        self.assertNotIn("Counter Comments Links", [x["label"] for x in m["categories"]])
        r = c.get("/api/daily", params={"day": m["data_through"]})
        self.assertEqual(r.status_code, 200)
        posts = r.json()["posts"]
        self.assertTrue(posts)
        self.assertTrue(all("run_id" not in p and "project_id" not in p for p in posts))
        r = c.get("/api/daily", params={"day": util.day_str(util.today_in("Asia/Kolkata"))})
        self.assertEqual(r.status_code, 400)                      # beyond the delay
        r = c.get("/api/trend", params={"from": "2000-01-01", "to": "2100-01-01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["to"], m["data_through"])
        r = c.get("/api/export.xlsx", params={"day": m["data_through"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content[:2] == b"PK")
        self.assertIn("Content-Security-Policy", r.headers)
        # sign out, then everything is gone again
        page = c.get("/").text
        import re
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = c.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = c.get("/api/meta")
        self.assertEqual(r.status_code, 401)
        # a wrong password never reveals whether the e-mail exists
        r = c.post("/login", data={"email": "person@example.com", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        r2 = c.post("/login", data={"email": "nobody@example.com", "password": "nope"})
        self.assertEqual(r2.status_code, 401)
        self.assertEqual(r.text.count("do not match"), r2.text.count("do not match"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

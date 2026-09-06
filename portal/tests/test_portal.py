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

from portal import schema, scraper, secretbox, util  # noqa: E402


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
               "sheet_date": "2026-09-03"}
        results = [
            {"status": "ok", "screenshot": "/nope/01.png", "post_link": "https://x.com/AbC/status/555?s=20",
             "platform": "x", "category": "National X Influencers", "handle": "AbC", "display_name": "A B C",
             "sheet_metrics": {"like": "1.2K", "views": "63,900"}},
            {"status": "login_wall", "screenshot": None, "post_link": "https://www.facebook.com/reel/77/",
             "platform": "facebook", "category": "Hyper Local Pages Posting", "handle": "", "account_name": "Page"},
        ]
        read = {"/nope/01.png": {"likes": 1180, "comments": 12, "shares": 40, "views": 63900, "engine": "tesseract"}}
        n = pp._publish_rows(conn, client, job, results, read, _dt.date(2026, 9, 3))
        conn.commit()
        self.assertEqual(n, 2)
        r = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://x.com/AbC/status/555'").fetchone()
        self.assertEqual(r["likes"], 1200)           # the sheet's typed number wins
        self.assertEqual(r["comments"], 12)          # OCR fills the blank
        self.assertEqual(r["views"], 63900)
        self.assertEqual(r["visible_from"], "2026-09-05")
        self.assertEqual(r["handle"], "@AbC")
        self.assertEqual(r["metric_source"], "mixed")
        s = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://facebook.com/reel/77'").fetchone()
        self.assertEqual(s["status"], "skipped")
        self.assertIsNone(s["likes"])
        # second publish of the same run updates, never duplicates
        n2 = pp._publish_rows(conn, client, job, results, read, _dt.date(2026, 9, 3))
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM post_metrics WHERE client_id='c1' AND run_id='job1'").fetchone()[0]
        self.assertEqual((n2, cnt), (2, 2))
        conn.close()

    def test_2_fold_scraper(self):
        from webapp import portal_publish as pp
        conn = schema.connect(os.environ["PORTAL_DB"])
        client = pp.client_get(conn, "c1")
        posts = scraper.normalize_many([
            {"platform": "x", "url": "https://twitter.com/AbC/status/555", "text": "hello", "created_at": "2026-09-03T10:00:00+05:30",
             "favorite_count": 1300, "retweet_count": 45, "reply_count": 13, "view_count": 70000},
            {"platform": "instagram", "url": "https://www.instagram.com/reel/NEW1/", "caption": "new", "taken_at": "2026-09-03T11:00:00+05:30",
             "like_count": 9, "comment_count": 1, "category": "Hyper Local Pages Posting"},
        ])
        matched, added = pp._fold_scraper(conn, client, {"signature_name": "t"}, posts, 2,
                                          _dt.date(2026, 9, 1), _dt.date(2026, 9, 4))
        conn.commit()
        self.assertEqual((matched, added), (1, 1))
        r = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://x.com/AbC/status/555'").fetchone()
        self.assertEqual(r["likes"], 1300)
        self.assertEqual(r["caption"], "hello")
        self.assertEqual(r["metric_source"], "scraper")
        n = conn.execute("SELECT * FROM post_metrics WHERE client_id='c1' AND post_url_norm='https://instagram.com/reel/NEW1'").fetchone()
        self.assertEqual(n["sheet_date"], "2026-09-03")
        self.assertEqual(n["visible_from"], "2026-09-05")
        conn.close()


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

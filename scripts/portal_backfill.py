#!/usr/bin/env python3
"""Publish every finished run of a client's projects into the Client Portal.

    .venv/bin/python scripts/portal_backfill.py                 # every client
    .venv/bin/python scripts/portal_backfill.py varanasi        # one client, by slug
    .venv/bin/python scripts/portal_backfill.py --sync          # also pull the scraper

No browser, no network unless --sync: it reads each job's results.json and
metrics_read.json off the disk and writes data/portal.db. Safe to re-run —
rows are upserted on (client, link, sheet day).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp import portal_publish                      # noqa: E402
from webapp.jobs import store                          # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sync = "--sync" in sys.argv
    store.init()
    conn = portal_publish.connect()
    try:
        if args:
            rows = [dict(r) for r in conn.execute("SELECT id, slug, name FROM clients WHERE slug IN (%s)" %
                                                  ",".join("?" * len(args)), args).fetchall()]
        else:
            rows = [dict(r) for r in conn.execute("SELECT id, slug, name FROM clients WHERE archived = 0").fetchall()]
    finally:
        conn.close()
    if not rows:
        print("No clients found." + (f" (asked for: {', '.join(args)})" if args else ""))
        return 1
    for c in rows:
        r = portal_publish.backfill_client(c["id"], by_user="backfill")
        print(f"{c['name']} ({c['slug']}): published {r['posts']} posts from {r['runs']} runs")
        if sync:
            s = portal_publish.sync_client(c["id"], by_user="backfill")
            print(f"  scraper: {s['posts']} posts fetched, {s['matched']} matched, {s['added']} added"
                  + (f"; errors: {'; '.join(s['errors'])}" if s["errors"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

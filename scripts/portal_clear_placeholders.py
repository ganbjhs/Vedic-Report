#!/usr/bin/env python3
"""Clear numbers that were never a measurement.

Some campaigns type a placeholder into the sheet's metric columns — the same
figure repeated down every row — as a layout stand-in. Published, those become
numbers a client reads as real. This removes them.

It removes the FIGURES, never the posts: the row keeps its link, day, category,
handle and caption, and its counts go back to NULL, which the dashboard prints
as a dash. A dash is true. A placeholder is not.

    python3 scripts/portal_clear_placeholders.py                  # dry run: say what is there
    python3 scripts/portal_clear_placeholders.py --apply          # clear them
    python3 scripts/portal_clear_placeholders.py --apply --untrust
                       # ...and switch the client's "trust the sheet's typed
                       # numbers" off, so the next hourly sheet sync does not
                       # simply put them back

Scraper numbers are never touched: only rows whose metric_source is one of
--source (default: sheet) are in scope.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal import config, schema                                    # noqa: E402

COUNTS = ("likes", "comments", "shares", "views", "reach", "impressions",
          "quotes", "bookmarks")


def _clients(conn, slug: str):
    sql = "SELECT id, slug, name, trust_sheet_metrics FROM clients"
    args = ()
    if slug:
        sql += " WHERE slug = ?"
        args = (slug,)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def report(conn, client: dict, sources: tuple) -> int:
    marks = ",".join("?" * len(sources))
    n = conn.execute(f"SELECT COUNT(*) FROM post_metrics WHERE client_id = ? "
                     f"AND metric_source IN ({marks})",
                     (client["id"],) + sources).fetchone()[0]
    print(f"\n{client['slug']}  ({client['name']})   trust_sheet_metrics="
          f"{client.get('trust_sheet_metrics', 1)}")
    print(f"  rows sourced from {'/'.join(sources)}: {n}")
    if not n:
        return 0
    print("  the repeated value sets (a placeholder shows up as one set on many rows):")
    rows = conn.execute(
        f"SELECT likes, comments, shares, views, reach, impressions, COUNT(*) AS n "
        f"FROM post_metrics WHERE client_id = ? AND metric_source IN ({marks}) "
        f"GROUP BY 1,2,3,4,5,6 ORDER BY n DESC LIMIT 10",
        (client["id"],) + sources).fetchall()
    for r in rows:
        vals = ", ".join(f"{k}={r[k]}" for k in
                         ("likes", "comments", "shares", "views", "reach", "impressions")
                         if r[k] is not None) or "(all blank)"
        print(f"    {r['n']:5} row(s)   {vals}")
    days = conn.execute(
        f"SELECT MIN(sheet_date), MAX(sheet_date) FROM post_metrics WHERE client_id = ? "
        f"AND metric_source IN ({marks})", (client["id"],) + sources).fetchone()
    print(f"  spanning {days[0]} .. {days[1]}")
    hist = conn.execute(
        f"SELECT COUNT(*) FROM post_metric_days WHERE client_id = ? AND metric_source IN ({marks})",
        (client["id"],) + sources).fetchone()[0]
    print(f"  history rows that would go with them: {hist}")
    return n


def clear(conn, client: dict, sources: tuple, untrust: bool) -> tuple:
    marks = ",".join("?" * len(sources))
    sets = ", ".join(f"{c} = NULL" for c in COUNTS)
    cur = conn.execute(
        f"UPDATE post_metrics SET {sets}, metric_source = 'none' "
        f"WHERE client_id = ? AND metric_source IN ({marks})",
        (client["id"],) + sources)
    cleared = cur.rowcount
    cur = conn.execute(
        f"DELETE FROM post_metric_days WHERE client_id = ? AND metric_source IN ({marks})",
        (client["id"],) + sources)
    history = cur.rowcount
    if untrust:
        conn.execute("UPDATE clients SET trust_sheet_metrics = 0 WHERE id = ?", (client["id"],))
    conn.commit()
    return cleared, history


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client", default="", help="one client slug (default: every client)")
    ap.add_argument("--source", default="sheet",
                    help="comma-separated metric_source values to clear (default: sheet). "
                         "'scraper' is refused — those are real readings.")
    ap.add_argument("--apply", action="store_true", help="actually write; without it this only reports")
    ap.add_argument("--untrust", action="store_true",
                    help="also switch the client's trust_sheet_metrics off, so the next sheet "
                         "sync does not re-publish the same figures")
    a = ap.parse_args()

    sources = tuple(s.strip() for s in a.source.split(",") if s.strip())
    if "scraper" in sources:
        print("Refusing: 'scraper' rows are real readings from the Collector, not placeholders.")
        return 2
    if not sources:
        print("Give at least one --source.")
        return 2

    schema.ensure_schema(config.PORTAL_DB)
    conn = schema.connect(config.PORTAL_DB)
    try:
        clients = _clients(conn, a.client)
        if not clients:
            print("No such client." if a.client else "No clients.")
            return 1
        print(f"database: {config.PORTAL_DB}")
        total = sum(report(conn, c, sources) for c in clients)
        if not a.apply:
            print(f"\nDry run — nothing written. {total} row(s) would have their numbers cleared.")
            print("Back up first, then re-run with --apply:")
            print(f"  cp {config.PORTAL_DB} {config.PORTAL_DB}.bak")
            print("  python3 scripts/portal_clear_placeholders.py --apply --untrust")
            return 0
        print()
        for c in clients:
            cleared, history = clear(conn, c, sources, a.untrust)
            note = ", trust_sheet_metrics switched off" if a.untrust else ""
            print(f"{c['slug']}: cleared {cleared} row(s), removed {history} history row(s){note}")
        print("\nThe posts are all still there — only the figures are gone. Anything the "
              "Collector has read will fill back in on the next sync.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

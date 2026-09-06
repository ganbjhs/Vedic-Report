"""What the dashboard asks for: meta, one day, a trend. Read-only, always
through `db.visible_posts` / the VISIBLE clause, always for the client in the
session.

Categories are the sheet's own headings (`category_raw`). The client's
`category_map` may relabel, reorder or hide them; a hidden category is
removed from every answer here, not just from the menu.
"""
import datetime as _dt

from . import config, db, util

PLATFORMS = ("facebook", "instagram", "x")
PLATFORM_NAMES = {"facebook": "Facebook", "instagram": "Instagram", "x": "X (Twitter)"}


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
def categories(client: dict) -> list:
    """[{raw, label, order, hidden}] in display order — every heading ever
    published for this client, mapped through client.category_map."""
    seen = db.rows(f"SELECT category_raw, MIN(id) AS first_id, COUNT(*) AS n FROM post_metrics "
                   f"WHERE {db.VISIBLE} GROUP BY category_raw ORDER BY first_id",
                   db.visible_params(client))
    cmap = client.get("category_map") or {}
    out, order_fallback = [], 0
    for r in seen:
        raw = r["category_raw"] or ""
        m = cmap.get(raw) or {}
        order_fallback += 1
        out.append({"raw": raw, "label": (m.get("label") or raw or "Uncategorised"),
                    "order": int(m.get("order") if m.get("order") not in (None, "") else 1000 + order_fallback),
                    "hidden": bool(m.get("hidden")), "posts": r["n"]})
    # headings that are only in the map (not yet published) are not listed —
    # a category with no posts has nothing to show
    out.sort(key=lambda c: (c["order"], c["label"]))
    return out


def visible_categories(client: dict) -> list:
    return [c for c in categories(client) if not c["hidden"]]


def _hidden_raw(client: dict) -> set:
    return {c["raw"] for c in categories(client) if c["hidden"]}


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
def meta(client: dict) -> dict:
    lo, hi = db.data_bounds(client)
    today = db.today_for(client)
    return {
        "client": {"name": client["name"], "display_name": client.get("display_name") or client["name"],
                   "logo": bool(client.get("logo_path")), "accent": client.get("accent_hex") or "",
                   "tz": client.get("tz") or config.PORTAL_TZ},
        "lag_days": int(client.get("lag_days") or config.PORTAL_LAG_DAYS_DEFAULT),
        "today": util.day_str(today),
        "data_from": lo, "data_through": hi,
        "default_day": hi,
        "categories": [{"raw": c["raw"], "label": c["label"]} for c in visible_categories(client)],
        "platforms": [{"id": p, "name": PLATFORM_NAMES[p]} for p in PLATFORMS],
        "agency": config.AGENCY_NAME,
        "show_screenshots": bool(client.get("show_screenshots")),
    }


# --------------------------------------------------------------------------- #
# One day
# --------------------------------------------------------------------------- #
def _post_public(r: dict, client: dict) -> dict:
    """The post as the client sees it. No run id, no project id, no screenshot
    path (a /media token instead when the client may see screenshots)."""
    return {
        "id": r["id"], "date": r["sheet_date"], "platform": r["platform"],
        "category": r["category_raw"] or "",
        "name": r.get("display_name") or r.get("handle") or "",
        "handle": r.get("handle") or "",
        "avatar": r.get("avatar_url") or "",
        "text": r.get("caption") or "",
        "media_type": r.get("media_type") or "",
        "thumb": r.get("thumb_url") or "",
        "posted_at": r.get("posted_at") or "",
        "collected_at": r.get("collected_at") or "",
        "url": r["post_url"],
        "likes": r.get("likes"), "comments": r.get("comments"), "shares": r.get("shares"),
        "views": r.get("views"),
        "metric_source": r.get("metric_source") or "none",
        "screenshot": (f"/media/{r['id']}" if client.get("show_screenshots") and r.get("screenshot_path") else ""),
    }


def daily(client: dict, day: str) -> dict:
    d = util.parse_day(day)
    if not d:
        raise ValueError("day must be YYYY-MM-DD")
    day = util.day_str(d)
    hidden = _hidden_raw(client)
    rows = db.visible_posts(client, "AND sheet_date = ? ORDER BY id", (day,))
    posts = [_post_public(r, client) for r in rows if (r["category_raw"] or "") not in hidden]
    return {"day": day, "posts": posts}


# --------------------------------------------------------------------------- #
# Trend — aggregates per day × category × platform
# --------------------------------------------------------------------------- #
def trend(client: dict, day_from: str, day_to: str) -> dict:
    a, b = util.parse_day(day_from), util.parse_day(day_to)
    if not a or not b:
        raise ValueError("from and to must be YYYY-MM-DD")
    if a > b:
        a, b = b, a
    lo, hi = db.data_bounds(client)
    if hi and util.day_str(b) > hi:
        b = util.parse_day(hi)
    if lo and util.day_str(a) < lo:
        a = util.parse_day(lo)
    if (b - a).days + 1 > config.MAX_TREND_DAYS:
        a = b - _dt.timedelta(days=config.MAX_TREND_DAYS - 1)
    hidden = _hidden_raw(client)
    rows = db.rows(
        f"SELECT sheet_date AS day, category_raw AS category, platform, COUNT(*) AS posts, "
        f"SUM(COALESCE(likes,0)) AS likes, SUM(COALESCE(comments,0)) AS comments, "
        f"SUM(COALESCE(shares,0)) AS shares, SUM(COALESCE(views,0)) AS views, "
        f"SUM(COALESCE(likes,0)+COALESCE(comments,0)+COALESCE(shares,0)) AS engagement "
        f"FROM post_metrics WHERE {db.VISIBLE} AND sheet_date BETWEEN ? AND ? "
        f"GROUP BY sheet_date, category_raw, platform ORDER BY sheet_date",
        db.visible_params(client) + (util.day_str(a), util.day_str(b)))
    rows = [r for r in rows if (r["category"] or "") not in hidden]
    days = [util.day_str(a + _dt.timedelta(days=i)) for i in range((b - a).days + 1)]
    return {"from": util.day_str(a), "to": util.day_str(b), "days": days, "rows": rows}

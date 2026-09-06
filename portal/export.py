"""The Excel workbook a client downloads: Day · Day summary · Trend · Growth.
Same figures as the page (built from the same queries), openpyxl, in memory.
"""
import datetime as _dt
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import queries, util

_HEAD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="F1EEEA")


def _sheet(wb, title, header, rows, widths=None):
    ws = wb.create_sheet(title[:31])
    ws.append(header)
    for c in ws[1]:
        c.font = _HEAD
        c.fill = _FILL
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    return ws


def _eng(p):
    return (p.get("likes") or 0) + (p.get("comments") or 0) + (p.get("shares") or 0)


def build(client: dict, day: str, day_from: str, day_to: str, growth_from: str = "",
          growth_to: str = "", metric: str = "engagement", split: str = "platform") -> bytes:
    m = queries.meta(client)
    d = queries.daily(client, day)
    t = queries.trend(client, day_from, day_to)
    labels = {c["raw"]: c["label"] for c in queries.categories(client)}
    plat = queries.PLATFORM_NAMES
    wb = Workbook()
    wb.remove(wb.active)

    # Day
    posts = sorted(d["posts"], key=lambda p: (labels.get(p["category"], p["category"]), -_eng(p)))
    _sheet(wb, "Day",
           ["Date", "Category", "Platform", "Name", "Handle", "Caption", "Likes", "Comments", "Shares",
            "Views", "Engagement", "Posted at", "Link"],
           [[p["date"], labels.get(p["category"], p["category"]), plat.get(p["platform"], p["platform"]),
             p["name"], p["handle"], p["text"], p["likes"], p["comments"], p["shares"], p["views"],
             _eng(p), p["posted_at"], p["url"]] for p in posts],
           [11, 28, 12, 22, 20, 50, 8, 10, 8, 10, 11, 22, 60])

    # Day summary
    summary = []
    for cat in queries.visible_categories(client):
        for pl in queries.PLATFORMS:
            r = [p for p in posts if p["category"] == cat["raw"] and p["platform"] == pl]
            if not r:
                continue
            summary.append([cat["label"], plat[pl], len(r),
                            sum(p["likes"] or 0 for p in r), sum(p["comments"] or 0 for p in r),
                            sum(p["shares"] or 0 for p in r), sum(p["views"] or 0 for p in r),
                            sum(_eng(p) for p in r)])
    summary.append(["All", "", len(posts), sum(p["likes"] or 0 for p in posts),
                    sum(p["comments"] or 0 for p in posts), sum(p["shares"] or 0 for p in posts),
                    sum(p["views"] or 0 for p in posts), sum(_eng(p) for p in posts)])
    ws = _sheet(wb, "Day summary",
                ["Category", "Platform", "Posts", "Likes", "Comments", "Shares", "Views", "Engagement"],
                summary, [28, 12, 7, 9, 10, 9, 10, 11])
    ws.append([])
    ws.append(["Day", d["day"]])
    ws.append(["Exported", util.day_str(_dt.date.today())])
    ws.append(["Reports are published with a delay of", f"{m['lag_days']} day(s)"])

    # Trend
    _sheet(wb, "Trend",
           ["Date", "Category", "Platform", "Posts", "Likes", "Comments", "Shares", "Views", "Engagement"],
           [[r["day"], labels.get(r["category"], r["category"]), plat.get(r["platform"], r["platform"]),
             r["posts"], r["likes"], r["comments"], r["shares"], r["views"], r["engagement"]]
            for r in t["rows"]],
           [11, 28, 12, 7, 9, 10, 9, 10, 11])

    # Growth
    g = queries.trend(client, growth_from or day_from, growth_to or day_to)
    n = len(g["days"])
    first = util.parse_day(g["from"])
    prev = queries.trend(client, util.day_str(first - _dt.timedelta(days=n)),
                         util.day_str(first - _dt.timedelta(days=1)))
    pn = len({r["day"] for r in prev["rows"]}) or len(prev["days"])
    key = "platform" if split == "platform" else "category"
    names = plat if split == "platform" else labels
    series = sorted({r[key] for r in g["rows"]} | {r[key] for r in prev["rows"]})
    rows = []
    for s in series:
        cur = [r for r in g["rows"] if r[key] == s]
        old = [r for r in prev["rows"] if r[key] == s]
        tot = sum(r.get(metric, 0) or 0 for r in cur)
        ptot = sum(r.get(metric, 0) or 0 for r in old)
        a = tot / n if n else 0
        b = (ptot / pn) if pn else None
        chg = round((a - b) / b * 100) if b else ""
        rows.append([names.get(s, s), sum(r["posts"] for r in cur), tot, ptot, round(a),
                     round(b) if b is not None else "", chg])
    ws = _sheet(wb, "Growth",
                ["Series", "Posts", f"{metric.title()} (period)", f"{metric.title()} (previous)",
                 "Per day (period)", "Per day (previous)", "Change % (per day)"],
                rows, [28, 8, 20, 20, 16, 18, 18])
    ws.append([])
    ws.append(["Period", f"{g['from']} to {g['to']} ({n} days)"])
    ws.append(["Previous", f"{prev['from']} to {prev['to']} ({pn} days with data)"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

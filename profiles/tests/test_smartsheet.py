"""v3: the smart sheet reader — the shapes seen in the team's real workbook.

Zero network: grids are pasted here as the CSV export returns them.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("APP_USERS", "t:tttttttt")
os.environ.setdefault("SESSION_SECRET", "x" * 40)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "profiles"))

from webapp import smartsheet as ss      # noqa: E402
from webapp import uploads               # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def main():
    print("\n1. a day tab: one column, yellow headings, blank rows")
    g = [["3 Party Pages Posting"], ["https://www.facebook.com/share/r/1JnNCGN9R6/"],
         ["https://www.instagram.com/reel/DbDpJmyyjFg/?igsh=MXNqdnV6MDJyN2xtNQ=="], [""],
         ["Hyper Local Pages Posting"], ["https://www.facebook.com/61559555815073/posts/pfbid0ZFhcZ518/"],
         [""], ["National X Influencers"], ["https://x.com/i/status/2079565117994524766"],
         ["https://x.com/befittingfacts/status/2079573589947564452?s=46"], ["Tweet Amplification Links"],
         ["https://x.com/i/status/2079611046441230657"]]
    u = ss.understand(g)
    check(len(u["posts"]) == 6, f"6 links ({len(u['posts'])})")
    check(u["sections"] == ["3 Party Pages Posting", "Hyper Local Pages Posting",
                            "National X Influencers", "Tweet Amplification Links"], "4 sections in order")
    check(u["posts"][2]["section"] == "Hyper Local Pages Posting", "link takes the heading above it")
    check(u["grid"][0][:3] == ["Section", "Handle", "Link"], "canonical header")
    rep = uploads.analyse(u["grid"], True, "combined")
    check(len(rep["rows"]) == 6, "uploads.analyse reads the canonical grid (combined)")
    check(rep["rows"][0]["category"] == "3 Party Pages Posting", "section survives into rows")
    check({r["platform"] for r in rep["rows"]} == {"facebook", "instagram", "x"}, "per-row platform")

    print("\n2. Tweet Links: date headings down A, links in B, unnamed number in C")
    g = [["Date- 4-7-26 Sr No.", "Links", ""], ["", "https://x.com/anujakapurindia/status/2073389948066214090", "8658"],
         ["", "https://x.com/AmritaRathodBJP/status/2073390100290158949", "8561"],
         ["Date- 5-7-26", "", ""], ["", "https://x.com/a/status/2073390255927926937", "8948"],
         ["Date 6-7-26", "", ""], ["", "https://x.com/b/status/2073390255927926999", "100"]]
    u = ss.understand(g)
    check(u["dates"] == ["2026-07-04", "2026-07-05", "2026-07-06"], f"three date blocks {u['dates']}")
    check(u["latest_date"] == "2026-07-06", "newest date")
    check(u["unnamed_numbers"] == 4 and any("no column heading" in n for n in u["notes"]),
          "unnamed number column is reported, not invented")
    lb = ss.latest_block(u)
    check(len(lb["posts"]) == 1 and lb["posts"][0]["date"] == "2026-07-06", "latest block = newest date only")

    print("\n3. Counter Links: several links in ONE cell")
    g = [["Date- 4-7-26 Sr No.", "Links https://x.com/i/status/2073309292883788043\nhttps://x.com/i/status/2073336419888898054 https://x.com/i/status/2073336205547131207"]]
    u = ss.understand(g)
    check(len(u["posts"]) == 3, "three links from one cell")

    print("\n4. 3rd Party Posting: header row, links without https://")
    g = [["Date", "Link"], ["14 Jul", "facebook.com/share/p/1BRuds93cE/"],
         ["14 Jul", "instagram.com/reel/DaxpmhHTwM2/"], ["15 Jul", "x.com/i/status/1"]]
    u = ss.understand(g, default_year=2026)
    check(u["shape"] == "headered", "header recognised")
    check(all(p["link"].startswith("https://") for p in u["posts"]), "scheme added")
    check(u["latest_date"] == "2026-07-15" and u["posts"][0]["date"] == "2026-07-14", "dates from the Date column")

    print("\n5. a proper sheet: Section | Handle | Link | Like | Post Impression")
    g = [["Section", "Handle", "Link", "Like", "Post Impression"],
         ["Ganga Aarti", "@kashi", "https://x.com/kashi/status/1", "676", "63,900"],
         ["", "@kashi2", "https://x.com/kashi/status/2", "10", "200"]]
    u = ss.understand(g)
    check(u["metric_names"] == ["Like", "Impressions"], f"metrics named from header {u['metric_names']}")
    check(u["grid"][2][0] == "Ganga Aarti", "section carries down blank cells")
    rep = uploads.analyse(u["grid"], False, "combined")
    check(rep["rows"][0].get("sheet_metrics", {}).get("impressions") == "63,900",
          "metrics reach analyse() (combined engine; the frozen X reader prints none by design)")

    print("\n6. dates + tabs")
    check(str(ss.parse_date("17/8/26")) == "2026-08-17", "17/8/26")
    check(str(ss.parse_date("24/7/25")) == "2025-07-24", "24/7/25")
    check(ss.parse_date("Tweet LInks") is None and ss.parse_date("3rd Party Posting") is None, "names are not dates")
    tabs = [{"name": "Tweet LInks", "gid": "0", "date": None}, {"name": "17/8/26", "gid": "1", "date": "2026-08-17"},
            {"name": "16/8/26", "gid": "2", "date": "2026-08-16"}]
    check(ss.newest_date_tab(tabs)["gid"] == "1", "newest date tab")
    html = 'var items = [];items.push({name: "Tweet LInks", pageUrl: "x", gid: "0",initialSheet: ("0" == gid)});items.push({name: "17\\/8\\/26", pageUrl: "y", gid: "613291519",initialSheet: false});'
    pairs = [(ss._unescape(m.group(1)), m.group(2)) for m in ss._ITEMS.finditer(html)]
    check(pairs == [("Tweet LInks", "0"), ("17/8/26", "613291519")], f"htmlview tab list parsed {pairs}")
    f1 = ss.fingerprint(u); u2 = ss.understand(g); check(f1 == ss.fingerprint(u2), "fingerprint is stable")
    g.append(["", "@new", "https://x.com/kashi/status/3", "1", "2"])
    check(ss.fingerprint(ss.understand(g)) != f1, "fingerprint changes when a link is added")

    if FAILS:
        print(f"\nFAILED: {len(FAILS)}")
        sys.exit(1)
    print("\nSMART SHEET OK — every shape of the team's workbook is read without pointing at columns")


if __name__ == "__main__":
    main()

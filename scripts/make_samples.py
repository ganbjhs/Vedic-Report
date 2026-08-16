#!/usr/bin/env python3
"""Build one sample document per profile, for the RULEBOOK rule 3 human check.

Automated tests prove every profile *builds*. They cannot prove it *looks*
right — and twice this session an image was wrong while every assertion passed
(rule 20's parent loss, and contact-sheet's crop amputating a reply). So a human
opens one document per profile, once.

Takes NO captures: it reuses screenshots from a previous run and copies them
into the sample folder, so the samples stay reproducible after the source is
deleted.

    .venv/bin/python scripts/make_samples.py --from <dir containing results.json>
    .venv/bin/python scripts/make_samples.py            # auto-find a source

Output: data/samples/ (gitignored), one <profile>.<ext> per declared output.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "profiles"))

import prof_builder     # noqa: E402
import registry         # noqa: E402

SAMPLES = ROOT / "data" / "samples"
# The fixture lives OUTSIDE data/samples and persists, so that once it has been
# seeded from a real run, `make_samples.py` with no arguments keeps working
# forever — no globbing of temp directories that move or get cleaned up.
FIXTURE = ROOT / "data" / "sample-fixture"

# Plausible stand-ins so the Influencer layout can be judged. The benchmark runs
# used the X engine, which does not collect metrics — these are NOT real
# numbers, and the note says so.
FAKE_METRICS = [
    {"followers": "12,400", "reactions": "318", "comments": "42", "reach": "58,900", "shares": "77"},
    {"followers": "3,910", "reactions": "96", "comments": "11", "reach": "14,200", "shares": "19"},
    {"followers": "88,200", "reactions": "2,140", "comments": "301", "reach": "412,000", "shares": "560"},
    {"followers": "640", "reactions": "23", "comments": "4", "reach": "3,100", "shares": "2"},
    {"followers": "21,050", "reactions": "487", "comments": "63", "reach": "97,400", "shares": "121"},
    {"followers": "5,730", "reactions": "142", "comments": "18", "reach": "22,800", "shares": "31"},
    {"followers": "154,000", "reactions": "5,020", "comments": "890", "reach": "1,100,000", "shares": "1,340"},
    {"followers": "2,480", "reactions": "61", "comments": "7", "reach": "9,600", "shares": "12"},
]

NOTES = {
    "twitter": [
        "One post per page, letter, image centred at the top with the link under it.",
        "Compare against twitter-REFERENCE-frozen.pdf: image size and position must match page for page. The trailing Links table may land differently — that part is not claimed identical.",
    ],
    "influencer": [
        "A4, TWO posts per page side by side, five metric rows under each.",
        "Metric VALUES here are placeholders (the source run used the X engine); judge the layout, not the numbers.",
    ],
    "contact-sheet": [
        "SIX posts per page in a 2x3 grid, letter, page number in the footer.",
        "The one that matters: no post may be missing its reply — every tile is padded to 4:5, never cropped.",
    ],
    "client-deck": [
        "A4 with a cover page first, then one padded 4:5 card per page, rounded corners + hairline border + soft shadow.",
        "Check the card is centred with even white padding and nothing is clipped at the rounded corners.",
    ],
}


def resolvable(src: Path) -> int:
    """How many of this source's results have a screenshot we can actually read.

    The existence of results.json and a screenshots/ folder is NOT enough:
    results.json holds absolute paths (correct, see RULEBOOK rule 2) which point
    into whatever directory produced it. `reports/results.json` in this repo
    still names a machine path that no longer exists, and an earlier version of
    this script happily "succeeded" on it while producing nothing at all.
    """
    rj = src / "results.json"
    if not rj.exists():
        return 0
    try:
        rows = json.loads(rj.read_text())
    except (ValueError, OSError):
        return 0
    count = 0
    for r in rows:
        if r.get("status") != "ok" or not r.get("screenshot"):
            continue
        name = Path(r["screenshot"]).name
        if Path(r["screenshot"]).exists() or (src / "screenshots" / name).exists():
            count += 1
    return count


def find_source() -> Path:
    """The candidate with the most readable screenshots, or a clear error."""
    candidates = [FIXTURE]                       # the persistent seeded fixture
    candidates += sorted((ROOT / "data" / "acceptance").glob("*/reports"))
    candidates += sorted((ROOT / "data" / "jobs").glob("*/app/reports"))
    candidates.append(ROOT / "reports")

    scored = [(resolvable(c), c) for c in candidates if c.is_dir()]
    scored = [(n, c) for n, c in scored if n > 0]
    if not scored:
        looked = "\n".join(f"    {c}" + ("" if c.is_dir() else "  (missing)")
                           for c in candidates)
        raise SystemExit(
            "No usable source found. A source needs a results.json whose\n"
            "screenshots can actually be read — results.json holds ABSOLUTE\n"
            "paths, so one written on another machine or in a deleted temp dir\n"
            "resolves to nothing.\n\n"
            f"Looked in:\n{looked}\n\n"
            "Pass one explicitly:\n"
            "    .venv/bin/python scripts/make_samples.py --from <dir>\n"
            "where <dir> contains results.json and screenshots/.")
    scored.sort(key=lambda t: -t[0])
    best_n, best = scored[0]
    print(f"auto-selected source with {best_n} readable screenshot(s)")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=None)
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    src = Path(args.src) if args.src else find_source()
    print(f"source: {src}")

    if not (src / "results.json").exists():
        raise SystemExit(f"{src} has no results.json")
    n_readable = resolvable(src)
    if n_readable == 0:
        raise SystemExit(
            f"{src} has a results.json but NONE of its screenshots can be read.\n"
            "Those paths are absolute (RULEBOOK rule 2) and point somewhere that\n"
            "no longer exists. Pick a directory from a run on this machine.")

    results = json.loads((src / "results.json").read_text())
    usable = [r for r in results if r.get("status") == "ok"][:args.limit]

    # Build into a staging dir and only replace data/samples once documents
    # actually exist. An earlier version wiped the good samples first and then
    # produced nothing, reporting success either way.
    staging = SAMPLES.with_name("samples.new")
    if staging.exists():
        shutil.rmtree(staging)
    stage_fixture = staging / "_fixture"
    (stage_fixture / "screenshots").mkdir(parents=True)

    fixture = []
    for i, r in enumerate(usable):
        name = Path(r["screenshot"]).name
        source_png = Path(r["screenshot"])
        if not source_png.exists():
            source_png = src / "screenshots" / name
        if not source_png.exists():
            continue
        shutil.copy2(source_png, stage_fixture / "screenshots" / name)
        rec = dict(r)
        rec["screenshot"] = str(stage_fixture / "screenshots" / name)
        rec["metrics"] = FAKE_METRICS[len(fixture) % len(FAKE_METRICS)]
        rec.setdefault("account_name", rec.get("handle") or f"Post {i + 1}")
        fixture.append(rec)

    if not fixture:
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(
            f"copied 0 screenshots from {src} — nothing to build.\n"
            "(results.json listed files, but none were readable.)")

    (stage_fixture / "results.json").write_text(json.dumps(fixture, indent=2))
    print(f"fixture: {len(fixture)} real screenshot(s) copied")

    made = {}
    for slug in registry.available():
        profile = registry.load(slug)
        prof_builder.OUT = stage_fixture
        sys.argv = ["prof_builder", f"{profile['label']} — sample", slug, slug]
        prof_builder.main()
        made[slug] = []
        for kind in profile["outputs"]:
            produced = stage_fixture / f"{slug}.{kind}"
            if produced.exists():
                dest = staging / f"{slug}.{kind}"
                shutil.move(str(produced), dest)
                made[slug].append(dest)

    # Reference documents from the FROZEN builders on the SAME fixture, so
    # "does the profile reproduce the existing report" is a side-by-side
    # comparison rather than a memory test.
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "influencer"))
    import report_builder as FX          # noqa: E402  frozen, read-only
    import inf_report_builder as FI      # noqa: E402  parallel impl, read-only
    import tempfile                      # noqa: E402

    rows = [r for r in fixture if FX._usable(r)]
    with tempfile.TemporaryDirectory() as td:
        embed = FX._compress_for_embed(rows, td)
        FX.build_pdf(embed, "Twitter Report — sample",
                     staging / "twitter-REFERENCE-frozen.pdf")
        FX.build_docx(embed, "Twitter Report — sample",
                      staging / "twitter-REFERENCE-frozen.docx")
    with tempfile.TemporaryDirectory() as td:
        embed = FI._compress_for_embed(rows, td)
        FI.build_pdf(embed, "Influencer Report — sample",
                     staging / "influencer-REFERENCE-frozen.pdf")
    print("\nreferences from the frozen builders written for comparison")

    lines = ["# Sample documents — RULEBOOK rule 3 human check",
             "",
             f"Built from {len(fixture)} REAL screenshots (no new captures).",
             "Open each one and check the two lines under its name.",
             "",
             "`*-REFERENCE-frozen.*` come from the existing `src/report_builder.py`",
             "and `influencer/inf_report_builder.py` on the SAME screenshots, so the",
             "profile output can be compared side by side rather than from memory.",
             "",
             "What parity claims: image size and position, page for page. It does NOT",
             "claim the trailing Links table lands on the same page — reportlab's",
             "platypus flows that by its own rules and the profile builder does not",
             "reimplement them.",
             ""]
    print("\n" + "=" * 68)
    for slug, paths in made.items():
        print(f"\n{slug}")
        lines.append(f"## {slug}")
        for note in NOTES.get(slug, []):
            print(f"   - {note}")
            lines.append(f"- {note}")
        lines.append("")
        for p in paths:
            size = p.stat().st_size / 1024
            print(f"   -> {p.name}  ({size:.0f} KB)")
            lines.append(f"  `{p.name}` ({size:.0f} KB)")
        lines.append("")
    (staging / "WHAT-TO-LOOK-FOR.md").write_text("\n".join(lines))
    # HARD GATE: never report success without documents on disk.
    built = sorted(p for p in staging.glob("*.*") if p.suffix != ".md")
    if not built:
        shutil.rmtree(staging, ignore_errors=True)
        print("\nFAILED: no documents were produced.", flush=True)
        return 1

    # Persist the fixture so the next bare run finds it (see FIXTURE above).
    if FIXTURE.resolve() != stage_fixture.resolve():
        if FIXTURE.exists():
            shutil.rmtree(FIXTURE)
        shutil.copytree(stage_fixture, FIXTURE)
        # rewrite the copied results.json to point at its new home
        rows = json.loads((FIXTURE / "results.json").read_text())
        for row in rows:
            row["screenshot"] = str(FIXTURE / "screenshots"
                                    / Path(row["screenshot"]).name)
        (FIXTURE / "results.json").write_text(json.dumps(rows, indent=2))

    if SAMPLES.exists():
        shutil.rmtree(SAMPLES)
    staging.rename(SAMPLES)

    print(f"\n{'=' * 68}")
    print(f"{len(built)} document(s) in: {SAMPLES}")
    for p in built:
        print(f"   {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

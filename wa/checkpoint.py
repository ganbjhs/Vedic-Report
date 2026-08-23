#!/usr/bin/env python3
"""
checkpoint.py — local, git-free version history for this project.

    python checkpoint.py save "what changed"      # snapshot -> checkpoints/NNN-slug.zip + entry in CHECKPOINTS.md
    python checkpoint.py list                     # show all checkpoints
    python checkpoint.py diff                     # files changed since the last checkpoint
    python checkpoint.py restore NNN              # restore files from checkpoint NNN (current state is saved first)

What is snapshotted: every source/config/doc file in the project.
What is NOT: wa_profile/ (your WhatsApp login), node_modules/, dist/, build/, checkpoints/, caches, secrets.
Rule (see RULEBOOK.md): every change to the project ends with `checkpoint.py save "<message>"`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CP_DIR = ROOT / "checkpoints"
LOG = ROOT / "CHECKPOINTS.md"
MANIFEST = CP_DIR / "manifest.json"

EXCLUDE_DIRS = {"wa_profile", "node_modules", "dist", "build", "checkpoints", "__pycache__", ".git",
                ".venv", ".venv-build", ".pytest_cache"}
EXCLUDE_FILES = {"service_account.json", "debug.png", "links.csv", ".DS_Store"}
EXCLUDE_SUFFIX = {".pyc", ".log", ".zip", ".spec.bak"}


def files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIX:
            continue
        out.append(p)
    return sorted(out)


def digest(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def snapshot_hashes() -> dict[str, str]:
    return {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in files()}


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"checkpoints": []}


def save_manifest(m: dict) -> None:
    CP_DIR.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


def changes(prev: dict[str, str], cur: dict[str, str]) -> dict[str, list[str]]:
    return {"added": sorted(k for k in cur if k not in prev),
            "modified": sorted(k for k in cur if k in prev and prev[k] != cur[k]),
            "deleted": sorted(k for k in prev if k not in cur)}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "checkpoint"


def cmd_save(message: str) -> None:
    m = load_manifest()
    prev = m["checkpoints"][-1]["hashes"] if m["checkpoints"] else {}
    cur = snapshot_hashes()
    ch = changes(prev, cur)
    if m["checkpoints"] and not any(ch.values()):
        print("Nothing changed since the last checkpoint.")
        return
    n = len(m["checkpoints"]) + 1
    name = f"{n:03d}-{slugify(message)}"
    CP_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(CP_DIR / f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in files():
            z.write(p, str(p.relative_to(ROOT)))
    stamp = time.strftime("%Y-%m-%d %H:%M")
    m["checkpoints"].append({"n": n, "name": name, "message": message, "time": stamp, "hashes": cur, "changes": ch})
    save_manifest(m)
    entry = [f"\n## {n:03d} — {message}", f"*{stamp}* · `checkpoints/{name}.zip`", ""]
    for k in ("added", "modified", "deleted"):
        if ch[k]:
            entry.append(f"- **{k}:** " + ", ".join(f"`{f}`" for f in ch[k]))
    if not LOG.exists():
        LOG.write_text("# Checkpoints\n\nLocal change history (newest at the bottom). Managed by `checkpoint.py`.\n",
                       encoding="utf-8")
    with LOG.open("a", encoding="utf-8") as f:
        f.write("\n".join(entry) + "\n")
    print(f"Saved checkpoint {name}  (+{len(ch['added'])} ~{len(ch['modified'])} -{len(ch['deleted'])})")


def cmd_list() -> None:
    m = load_manifest()
    if not m["checkpoints"]:
        print("No checkpoints yet.")
    for c in m["checkpoints"]:
        ch = c["changes"]
        print(f"{c['n']:03d}  {c['time']}  {c['message']}   (+{len(ch['added'])} ~{len(ch['modified'])} -{len(ch['deleted'])})")


def cmd_diff() -> None:
    m = load_manifest()
    prev = m["checkpoints"][-1]["hashes"] if m["checkpoints"] else {}
    ch = changes(prev, snapshot_hashes())
    for k in ("added", "modified", "deleted"):
        for f in ch[k]:
            print(f"{k[:1].upper()}  {f}")
    if not any(ch.values()):
        print("Clean — nothing changed since the last checkpoint.")


def cmd_restore(n: str) -> None:
    m = load_manifest()
    target = next((c for c in m["checkpoints"] if c["n"] == int(n)), None)
    if not target:
        sys.exit(f"No checkpoint {n}. Run `checkpoint.py list`.")
    cmd_save(f"auto-save before restoring {int(n):03d}")
    with zipfile.ZipFile(CP_DIR / f"{target['name']}.zip") as z:
        z.extractall(ROOT)
    print(f"Restored files from checkpoint {target['name']} (wa_profile and secrets untouched).")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
    elif args[0] == "save":
        cmd_save(" ".join(args[1:]) or "checkpoint")
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "diff":
        cmd_diff()
    elif args[0] == "restore" and len(args) > 1:
        cmd_restore(args[1])
    else:
        print(__doc__)

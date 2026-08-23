"""
Where things live — works both from source (python app.py) and as a frozen
PyInstaller app (.exe / .app).

  APP_DIR   read-only bundled resources: ui/, wa/, default config.json, tasks.json, plugins/
  DATA_DIR  writable user data: config.json, tasks.json, plugins/, wa_profile/, links.csv,
            service_account.json, debug.png
            source run  -> the project folder itself
            frozen      -> %APPDATA%\\WAToolkit  (Windows)
                           ~/Library/Application Support/WAToolkit  (macOS)
                           ~/.wa_toolkit  (Linux)
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

if FROZEN:
    if sys.platform == "win32":
        DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "WAToolkit"
    elif sys.platform == "darwin":
        DATA_DIR = Path.home() / "Library" / "Application Support" / "WAToolkit"
    else:
        DATA_DIR = Path.home() / ".wa_toolkit"
    # Windows/Linux: Playwright browsers are bundled inside the package (installed with
    # PLAYWRIGHT_BROWSERS_PATH=0 at build time). macOS: Chromium.app cannot be bundled by
    # PyInstaller (framework symlinks/codesign) -> downloaded on first launch to
    # ~/Library/Caches/ms-playwright by wa.session.ensure_browsers().
    if sys.platform != "darwin":
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
else:
    DATA_DIR = APP_DIR

# Server build: DATA_DIR must be a writable volume, not the read-only image.
_env_data_dir = os.environ.get("WA_DATA_DIR", "").strip()
if _env_data_dir:
    DATA_DIR = Path(_env_data_dir).expanduser().resolve()

DATA_DIR.mkdir(parents=True, exist_ok=True)


def ensure_defaults() -> None:
    """First run: copy default config/tasks/plugins into DATA_DIR.

    Applies whenever DATA_DIR is separate from the bundled APP_DIR — a frozen
    .exe/.app, or the server build with WA_DATA_DIR pointed at a volume. A plain
    source run has DATA_DIR == APP_DIR and is a no-op, exactly as before."""
    if DATA_DIR == APP_DIR:
        return
    for name in ("config.json", "tasks.json", "file1.txt", "file2.txt"):
        src, dst = APP_DIR / name, DATA_DIR / name
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
    src, dst = APP_DIR / "plugins", DATA_DIR / "plugins"
    if src.exists() and not dst.exists():
        shutil.copytree(src, dst)


ensure_defaults()
CONFIG = DATA_DIR / "config.json"
TASKS = DATA_DIR / "tasks.json"
UI_INDEX = APP_DIR / "ui" / "index.html"

# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for WA Toolkit (app + bot in one executable).
# Build:  pyinstaller packaging/WAToolkit.spec   (run from the project root)
import os, sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None
root = os.path.abspath(os.path.join(SPECPATH, ".."))

# playwright: python package + node driver + browsers (installed with PLAYWRIGHT_BROWSERS_PATH=0)
pw_datas, pw_bins, pw_hidden = collect_all("playwright")
wv_datas, wv_bins, wv_hidden = collect_all("webview")

datas = pw_datas + wv_datas + [
    (os.path.join(root, "ui"), "ui"),
    (os.path.join(root, "wa"), "wa"),
    (os.path.join(root, "plugins"), "plugins"),
    (os.path.join(root, "config.json"), "."),
    (os.path.join(root, "tasks.json"), "."),
    (os.path.join(root, "file1.txt"), "."),
    (os.path.join(root, "file2.txt"), "."),
]

a = Analysis(
    [os.path.join(root, "app.py")],
    pathex=[root],
    binaries=pw_bins + wv_bins,
    datas=datas,
    hiddenimports=pw_hidden + wv_hidden + ["bot", "gspread", "google.auth", "requests", "wa", "wa.session", "wa.chat",
                                           "wa.reader", "wa.links", "wa.sheets", "wa.metrics", "wa.engine", "wa.paths"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="WAToolkit",
    console=False,          # no console window; bot output is captured by the app
    icon=os.path.join(root, "packaging", "icon.ico") if os.path.exists(os.path.join(root, "packaging", "icon.ico")) else None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="WAToolkit")

if sys.platform == "darwin":
    app = BUNDLE(coll, name="WA Toolkit.app", bundle_identifier="dev.tilak.watoolkit",
                 icon=os.path.join(root, "packaging", "icon.icns") if os.path.exists(os.path.join(root, "packaging", "icon.icns")) else None,
                 info_plist={"NSHighResolutionCapable": True, "LSUIElement": False})

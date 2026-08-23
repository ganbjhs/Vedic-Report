#!/bin/sh
# Builds "dist/WA Toolkit.app" and dist/WAToolkit.dmg. Needs Python 3.11+ on the build Mac only.
set -e
cd "$(dirname "$0")/.."
python3 -m venv .venv-build && . .venv-build/bin/activate
pip install --upgrade pip && pip install -r requirements.txt pyinstaller
# macOS: browsers are downloaded by the app on first launch (Chromium.app can't be bundled by PyInstaller)
pyinstaller --noconfirm --clean packaging/WAToolkit.spec
rm -f dist/WAToolkit.dmg
hdiutil create -volname "WA Toolkit" -srcfolder "dist/WA Toolkit.app" -ov -format UDZO dist/WAToolkit.dmg
echo "Built: dist/WA Toolkit.app and dist/WAToolkit.dmg"

@echo off
REM Builds WAToolkit\WAToolkit.exe (folder) and, if Inno Setup is installed, WAToolkit-Setup.exe.
REM Needs: Python 3.11+ on the build machine only. End users need NOTHING installed.
cd /d "%~dp0\.."
python -m venv .venv-build || goto :err
call .venv-build\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :err
REM bundle Chromium inside the playwright package so PyInstaller picks it up
set PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install chromium || goto :err
pyinstaller --noconfirm --clean packaging\WAToolkit.spec || goto :err
echo.
echo Built dist\WAToolkit\WAToolkit.exe
where iscc >nul 2>nul && (
  iscc packaging\installer.iss && echo Installer: dist\WAToolkit-Setup.exe
) || echo Inno Setup (iscc) not found - skipping installer; zip the dist\WAToolkit folder instead.
goto :eof
:err
echo BUILD FAILED
exit /b 1

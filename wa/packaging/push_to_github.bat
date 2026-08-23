@echo off
REM One-shot: prepare this folder and push it to a NEW GitHub repo (Windows).
REM   packaging\push_to_github.bat https://github.com/<user>/<repo>.git
if "%~1"=="" ( echo usage: %~nx0 ^<git remote url^> & exit /b 1 )
cd /d "%~dp0\.."
if not exist .github\workflows mkdir .github\workflows
copy /y packaging\github-build.yml .github\workflows\build.yml >nul
if not exist .git git init -b main
git add -A
git commit -m "WA Toolkit"
git remote remove origin 2>nul
git remote add origin %1
git push -u origin main
echo Pushed. Now: GitHub -^> Actions -^> "Build installers" -^> Run workflow -^> download WAToolkit-Windows.

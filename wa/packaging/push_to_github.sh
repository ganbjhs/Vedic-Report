#!/bin/sh
# One-shot: prepare this folder and push it to a NEW GitHub repo (macOS/Linux).
#   ./packaging/push_to_github.sh https://github.com/<user>/<repo>.git
# Before running: create the empty repo on github.com (no README), and be logged in
# with that account in git (git config / credential helper / gh auth login).
set -e
[ -n "$1" ] || { echo "usage: $0 <git remote url>"; exit 1; }
cd "$(dirname "$0")/.."
mkdir -p .github/workflows && cp packaging/github-build.yml .github/workflows/build.yml
[ -d .git ] || git init -b main
git add -A
git commit -m "WA Toolkit" || true
git remote remove origin 2>/dev/null || true
git remote add origin "$1"
git push -u origin main
echo "Pushed. Now: GitHub -> Actions -> 'Build installers' -> Run workflow -> download WAToolkit-Windows / WAToolkit-macOS."

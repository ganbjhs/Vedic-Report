#!/usr/bin/env bash
# Deploy the latest main (or a tag: ./deploy.sh v2.4.0)
set -euo pipefail
cd "$(dirname "$0")"

REF="${1:-main}"
echo "== fetching"
git fetch --tags --prune origin
git checkout -q -- Dockerfile 2>/dev/null || true
if [ "$REF" = "main" ]; then
  git checkout -q main
  git pull -q --ff-only origin main
else
  git checkout -q "$REF"
fi
echo "== at: $(git log --oneline -1)  (VERSION $(cat VERSION))"

echo "== permissions"
chown -R 1000:1000 data sessions reports 2>/dev/null || true

echo "== build + restart"
docker compose up -d --build

echo "== waiting for health"
for i in $(seq 1 30); do
  if curl -fs localhost:8000/health >/dev/null 2>&1; then
    echo "OK: $(curl -s localhost:8000/health)"; docker compose ps; exit 0
  fi
  sleep 2
done
echo "!! health check failed — last logs:"; docker compose logs --tail=40 web; exit 1

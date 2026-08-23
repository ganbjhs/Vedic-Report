#!/usr/bin/env bash
# Deploy ONLY the WA Toolkit container. The report tool is never stopped,
# rebuilt or restarted by this script — `deploy.sh` next door still owns that.
#
#   ./wa/deploy-wa.sh
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root, where docker-compose.yml lives

echo "== wa/data (the WhatsApp login + config volume)"
mkdir -p wa/data
# The container runs as UID 1000. Deploying as root would leave this root-owned
# and the app cannot write config.json — the same trap the report tool has.
chown -R 1000:1000 wa/data 2>/dev/null || true

echo "== build + start wa (only this service)"
docker compose build wa
docker compose up -d wa

echo "== waiting for health"
for i in $(seq 1 30); do
  if curl -fs localhost:8010/health >/dev/null 2>&1; then
    echo "OK: $(curl -s localhost:8010/health)"
    break
  fi
  [ "$i" = 30 ] && { echo "!! health check failed — last logs:"; docker compose logs --tail=40 wa; exit 1; }
  sleep 2
done

echo "== validating Caddyfile BEFORE reloading (a bad config must not take the report tool down)"
docker compose --profile caddy exec -T caddy caddy validate \
  --config /etc/caddy/Caddyfile --adapter caddyfile

echo "== reloading Caddy in place (zero downtime, no container restart)"
docker compose --profile caddy exec -T caddy caddy reload \
  --config /etc/caddy/Caddyfile

echo
echo "done."
echo "  report tool : https://report.vedictech.in/      (untouched)"
echo "  wa toolkit  : https://report.vedictech.in/wa"
docker compose ps

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

echo "== edge proxy"
# This box may be fronted by the compose Caddy container OR by nginx installed
# on the host. Detect rather than assume — reloading the wrong one silently
# leaves /wa returning the report tool's 404.
if docker compose --profile caddy ps --status running --services 2>/dev/null | grep -qx caddy; then
  echo "   caddy container detected — validating before applying"
  docker compose --profile caddy exec -T caddy caddy validate \
    --config /etc/caddy/Caddyfile --adapter caddyfile
  docker compose --profile caddy exec -T caddy caddy reload \
    --config /etc/caddy/Caddyfile
  echo "   caddy reloaded (zero downtime)"
elif command -v nginx >/dev/null 2>&1; then
  if nginx -T 2>/dev/null | grep -q "location /wa/"; then
    echo "   nginx detected, /wa/ block present — testing and reloading"
    nginx -t && systemctl reload nginx
    echo "   nginx reloaded"
  else
    echo "   !! nginx is the edge on this box, and it has no '/wa/' location yet."
    echo "      /wa will keep returning the report tool's 404 until you add one."
    echo
    echo "      1. grep -rl report.vedictech.in /etc/nginx/"
    echo "      2. copy the block in wa/nginx-wa.conf into that server{},"
    echo "         above the catch-all 'location / {'"
    echo "      3. nginx -t && systemctl reload nginx"
    echo
    echo "   The wa container itself is up and healthy on 127.0.0.1:8010."
  fi
else
  echo "   !! neither a caddy container nor nginx found."
  echo "      Point your edge proxy at 127.0.0.1:8010, stripping the /wa prefix."
fi

echo
echo "done."
echo "  report tool : https://report.vedictech.in/      (untouched)"
echo "  wa toolkit  : https://report.vedictech.in/wa"
docker compose ps

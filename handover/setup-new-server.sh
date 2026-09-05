#!/usr/bin/env bash
# Run this ON THE NEW SERVER as root, inside the unzipped app folder (~/app),
# AFTER the state file from backup-old-server.sh has been copied to ~/.
#
#   cd ~/app && bash handover/setup-new-server.sh ~/vedicreport-state-*.tgz
#
# What it does, in order:
#   1. installs docker / compose / git / ufw if missing, opens ports 22 80 443
#   2. unpacks the state file over this folder (.env, logins, database ...)
#   3. sets WORKERS in .env from the RAM this machine has (override: --workers N)
#   4. fixes folder ownership (the containers run as UID 1000)
#   5. builds and starts everything, including Caddy (HTTPS) and the splitter
#      bot if tg/.env.splitter came across
#   6. waits for the health checks
#
# Change the DNS A record for report.vedictech.in to THIS server's IP right
# before running it — Caddy can only get the HTTPS certificate once the domain
# points here. It keeps retrying until it does, so early is harmless.
set -euo pipefail
cd "$(dirname "$0")/.."

STATE="${1:-}"
WORKERS_OVERRIDE=""
if [ "${2:-}" = "--workers" ] && [ -n "${3:-}" ]; then WORKERS_OVERRIDE="$3"; fi

if [ -z "$STATE" ] || [ ! -f "$STATE" ]; then
  echo "usage: bash handover/setup-new-server.sh <path to vedicreport-state-*.tgz> [--workers N]"; exit 1
fi
if [ "$(id -u)" -ne 0 ]; then echo "run as root (sudo -i)"; exit 1; fi

echo "== 1. packages"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-plugin git curl ufw
  systemctl enable --now docker
fi
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22 >/dev/null; ufw allow 80 >/dev/null; ufw allow 443 >/dev/null; ufw --force enable >/dev/null
fi
docker compose version

echo "== 2. unpacking state from $STATE"
tar xzf "$STATE" -C .
for f in .env tg/.env sessions/x_state.json data/jobs.db; do
  [ -e "$f" ] && echo "   ok  $f" || echo "   !!  missing $f (was it on the old server?)"
done

echo "== 3. WORKERS"
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if [ -n "$WORKERS_OVERRIDE" ]; then W="$WORKERS_OVERRIDE"
elif [ "$RAM_GB" -ge 15 ]; then W=8
elif [ "$RAM_GB" -ge 7 ]; then W=5
elif [ "$RAM_GB" -ge 3 ]; then W=3
else W=1; fi
OLD=$(grep -E '^WORKERS=' .env | head -1 || true)
if grep -qE '^WORKERS=' .env; then sed -i "s/^WORKERS=.*/WORKERS=$W/" .env; else echo "WORKERS=$W" >> .env; fi
echo "   ${RAM_GB} GB RAM -> ${OLD:-WORKERS=(unset)} becomes WORKERS=$W   (rule: one worker per 1-1.5 GB)"

echo "== 4. ownership"
mkdir -p sessions data reports wa/data tg/data tg/data-splitter
chown -R 1000:1000 sessions data reports wa/data tg/data tg/data-splitter

echo "== 5. build + start"
PROFILES=(--profile caddy)
if [ -f tg/.env.splitter ]; then PROFILES+=(--profile splitter); echo "   splitter bot: tg/.env.splitter present, starting it"; fi
docker compose "${PROFILES[@]}" up -d --build

echo "== 6. health"
ok=0
for i in $(seq 1 45); do
  if curl -fs localhost:8000/health >/dev/null 2>&1 && curl -fs localhost:8010/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" = 1 ]; then
  echo "   web: $(curl -s localhost:8000/health)"
  echo "   wa:  $(curl -s localhost:8010/health)"
else
  echo "!! health check failed — last logs:"; docker compose logs --tail=40 web wa; exit 1
fi
docker compose "${PROFILES[@]}" ps

cat <<'NEXT'

== next
  1. Make sure DNS  report.vedictech.in  now points at THIS server's IP.
  2. Watch Caddy get the certificate:   docker compose --profile caddy logs -f caddy
     (look for "certificate obtained"; Ctrl-C to stop watching)
  3. Open https://report.vedictech.in and https://report.vedictech.in/wa , log in, run one small report.
  4. Bot check:  docker compose logs --tail=30 tg   — must NOT show "Conflict: terminated by other getUpdates"
     (if it does, the old server is still running: stop it there).
  5. If X asks to log in again (new IP), follow README "4. Seed the X login".
NEXT

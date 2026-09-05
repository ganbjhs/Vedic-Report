#!/usr/bin/env bash
# Run this ON THE OLD SERVER, inside the app folder (usually ~/app).
#
#   cd ~/app && bash handover/backup-old-server.sh
#
# It STOPS the whole stack (web, wa, tg bots, caddy) and packs every piece of
# live state that is NOT in the code zip into one file:
#
#   .env, tg/.env, tg/.env.splitter      secrets / logins / bot tokens
#   sessions/                            the X (Twitter) login cookie
#   data/                                jobs.db + every job's files
#   reports/                             generated reports
#   wa/data/                             the WhatsApp login + WA config
#   tg/data/, tg/data-splitter/          what the Telegram bots remember
#   secrets/                             if present
#
# Stopping first is deliberate: jobs.db is SQLite and must not be copied while
# being written, and a Telegram bot token may only be polled by ONE process —
# two servers running `tg` at once = "Conflict: terminated by other getUpdates"
# and lost messages. The old server stays stopped; do not start it again unless
# you are rolling back.
set -euo pipefail
cd "$(dirname "$0")/.."

STAMP="$(date +%Y%m%d-%H%M)"
OUT="$HOME/vedicreport-state-$STAMP.tgz"

echo "== stopping the stack on this server"
docker compose --profile caddy --profile splitter stop

PATHS=()
for p in .env tg/.env tg/.env.splitter sessions data reports wa/data tg/data tg/data-splitter secrets; do
  if [ -e "$p" ]; then PATHS+=("$p"); else echo "   (skip, not here: $p)"; fi
done

echo "== packing: ${PATHS[*]}"
tar czf "$OUT" \
  --exclude='data/tmp' \
  --exclude='*/__pycache__' \
  --exclude='.DS_Store' \
  "${PATHS[@]}"

echo
echo "== done"
ls -lh "$OUT"
sha256sum "$OUT"
echo
echo "Now copy it to the new server, e.g.:"
echo "  scp $OUT root@<NEW_IP>:~/"
echo
echo "Roll back (only if the move fails): docker compose --profile caddy --profile splitter start"

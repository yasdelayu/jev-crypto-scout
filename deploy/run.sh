#!/usr/bin/env bash
# Scheduled run wrapper, called by the systemd timer.
# Builds scout.py's flags from config.json (botconfig.py) — the SAME settings
# the Telegram bot edits — so /top, /lang, /on, /off in chat also change what
# the timer does. Then logs to history.db and sends the Telegram digest.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# botconfig.to_argv turns stored settings into CLI flags; add --log --telegram.
ARGS=$(python3 -c "import botconfig; print(' '.join(botconfig.to_argv(botconfig.load())))")
exec python3 scout.py $ARGS --log --telegram

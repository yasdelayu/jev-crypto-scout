#!/usr/bin/env bash
# Scheduled run wrapper, called by the systemd timer (or cron).
# Loads secrets from /opt/jev-crypto-scout/.env, runs the full pipeline,
# logs to history.db and sends a Telegram digest. Fails quietly to the
# journal rather than spamming — a screening tool that errors shouldn't
# wake anyone.
set -euo pipefail
cd "$(dirname "$0")/.."

# .env holds TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, AI_GATEWAY_API_KEY —
# never committed (see .gitignore), 600 perms, owned by the run user.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# --top 100: screen the top 100 by market cap. --ta / --scalp are auto-limited
# to the ~30 most-moving of those (rate limits), --fng: market mood.
# --listings: Bybit new listings / delistings. --news --lang ru: Jev on
# Russian-language crypto feeds. --log: accumulate history. --telegram: deliver.
exec python3 scout.py --top 100 --ta --fng --scalp --listings --news --lang ru --log --telegram

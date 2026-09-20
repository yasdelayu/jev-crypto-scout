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

# --top 20: the screen. --ta: oscillators. --fng: market mood.
# --scalp: Bybit funding (works from this server's region, unlike some).
# --news: real Jev classification if the key is present.
# --log: accumulate history for validate.py. --telegram: deliver.
exec python3 scout.py --top 20 --ta --fng --scalp --news --log --telegram

# Deploy — scheduled runs on a server

This runs `scout.py` on a timer, logs each run to `history.db`, and delivers
a short digest to Telegram. Everything here is optional; the tool works fine
run by hand. This is for "I want it checking the market for me on a schedule."

## One-time setup

```bash
# 1. Clone (or pull) the repo
cd /opt && git clone https://github.com/yasdelayu/jev-crypto-scout.git
cd /opt/jev-crypto-scout

# 2. Secrets — never committed, 600 perms
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
AI_GATEWAY_API_KEY=vck_...
EOF
chmod 600 .env

# 3. Install the systemd timer
cp deploy/jev-scout.service /etc/systemd/system/
cp deploy/jev-scout.timer   /etc/systemd/system/
chmod +x deploy/run.sh
systemctl daemon-reload
systemctl enable --now jev-scout.timer
```

## Check it

```bash
systemctl list-timers jev-scout.timer      # when it next fires
systemctl start jev-scout.service          # run once, right now
journalctl -u jev-scout.service -n 40      # what the last run printed
```

The first manual `systemctl start` is the real test: it should send one
Telegram message and add ~20 rows to `history.db`.

## Finding your Telegram chat_id

Message your bot once (any text), then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
```

The `chat.id` in the response is your `TELEGRAM_CHAT_ID`.

## Schedule

`deploy/jev-scout.timer` fires every 4 hours (`OnCalendar=*-*-* 00/4:00:00`).
Edit that line and `systemctl daemon-reload && systemctl restart jev-scout.timer`
to change it. Every 4h is a deliberate middle ground: frequent enough to
accumulate history for `validate.py` and catch funding/news shifts, gentle
enough on free-tier API rate limits.

## Updating

```bash
cd /opt/jev-crypto-scout && git pull
# .env and history.db are gitignored, so they survive a pull untouched.
```

## Security notes

- `.env` and `history.db` are gitignored — they never reach GitHub.
- The Telegram bot token and the Jev key live only in `.env` on the server,
  600 perms. Rotate either if it ever leaks (BotFather `/revoke` for the bot,
  Vercel AI Gateway → API Keys for Jev).
- This deploys a read-only screening tool. It holds no exchange API keys,
  places no orders, and cannot move funds — by construction, not config.

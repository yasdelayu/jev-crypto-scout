#!/usr/bin/env python3
"""Interactive Telegram bot — long-polling, no dependency (plain Bot API).

Lets the owner change settings (coin count, language, which modules) and
trigger a run on demand, and explains what every symbol in the digest means.
Runs as a long-lived systemd service alongside the scheduled timer; both read
the same config.json (botconfig.py), so a change here applies to the next
scheduled run too.

Only the chat in TELEGRAM_CHAT_ID is honored — commands from anyone else are
ignored. Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / AI_GATEWAY_API_KEY from
the environment (loaded from .env by the service).
"""
import json, os, subprocess, sys, threading, time, urllib.parse, urllib.request

import botconfig

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = """<b>🤖 Jev Crypto Scout — бот</b>

Это скринер крипты: считает индикаторы в коде, а новости разбирает ИИ-модель Jev. Ничего не покупает и не продаёт — присылает сводку, решаешь ты.

<b>Команды:</b>
/quick — быстрая сводка (топ-20, ~1–2 мин)
/run — полный прогон (по настройкам, ~3–5 мин)
/settings — текущие настройки
/top 50 — сколько монет отслеживать (по капитализации)
/lang ru — язык новостей (ru или en)
/on scalp · /off scalp — включить/выключить блок
   блоки: <code>scalp fng listings ta news attention</code>
/legend — что значат все значки
/help — это сообщение

<b>Как читать сводку:</b>
🟢/🔴 у монеты — цена выросла/упала за 7 дней
📰 — новостной балл: сумма настроений новостей по монете (плюс — хорошие, минус — плохие)
⚠️ oversold — перепродано (RSI низкий, возможен отскок вверх)
⚠️ overbought — перекуплено (RSI высокий, возможна коррекция вниз)

<b>⚡ Фандинг</b> (бессрочные фьючерсы Bybit):
🔴 лонги платят — толпа в лонг, переплачивает шортам (сигнал перегрева)
🟢 шорты платят — толпа в шорт
z — насколько сильно перекос отклонён от нормы
🎯 — кандидат: фандинг И осциллятор экстремальны разом (редко)

<b>🧠 Новости (Jev):</b>
🟢 — Jev считает новость хорошей для цены
🔴 — плохой (взлом, запрет, иск)
[тип] — категория события (regulation, hack_exploit, listing, partnership…)
Заголовок кликабельный — ведёт на источник

<b>📈/📉</b> — листинг/делистинг монеты на бирже

<b>🎯 На что смотреть</b> — главное: Jev оценивает ситуацию по монете целиком (цена+фандинг+новости разом) и подсказывает шаг: 👀 наблюдать / 🔍 разобраться в причине. Это не совет купить, а куда направить внимание из сотни монет.

<i>Это не сигнал на сделку и не инвестсовет.</i>"""

LEGEND = """<b>ℹ️ Все значки сводки:</b>

<b>Монеты:</b>
🟢+5.2% / 🔴-3.1% — цена за 7 дней
📰+1.4 — новостной балл (хорошие минус плохие новости)
⚠️ oversold — RSI перепродан (дёшево относительно недавнего)
⚠️ overbought — RSI перекуплен (дорого)

<b>Фандинг</b> (кто переплачивает на фьючерсах):
🔴 лонги платят — толпа ставит на рост и платит за это
🟢 шорты платят — толпа ставит на падение
z=+2.1 — сила перекоса (>1.5 или <-1.5 = сильный)
🎯 — редкий двойной сигнал (фандинг + осциллятор)

<b>Новости:</b>
🟢 хорошо для цены / 🔴 плохо
[regulation] [hack_exploit] [listing] [partnership] [hype_speculation] — тип события

<b>Биржа:</b>
📈 новый листинг (свежая ликвидность/внимание)
📉 делистинг (риск)

<b>Настроение рынка:</b>
🌡 Fear&amp;Greed 0–100 (страх ↔ жадность)
DeFi TVL — сколько денег заблокировано в DeFi"""


def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def send(text):
    # reuse the splitter from telegram_notify so long replies don't hit the cap
    import telegram_notify
    telegram_notify.send_text(text)


_run_lock = threading.Lock()


def run_scout(quick=False):
    """Trigger a run; scout.py sends the digest itself via --telegram. Runs in
    a thread so polling keeps responding, reports failure instead of dying
    silently. quick=True: a fast profile (top-20, no slow daily oscillators)
    for 'just show me now' — the full config run takes several minutes."""
    if not _run_lock.acquire(blocking=False):
        send("⏳ Прогон уже идёт, подожди его окончания.")
        return
    try:
        if quick:
            # fast: skip --ta (sequential CoinGecko OHLC is the slow part),
            # keep the high-value bits — funding, news, listings, attention.
            argv = [sys.executable, os.path.join(HERE, "scout.py"),
                    "--top", "20", "--fng", "--scalp", "--listings",
                    "--news", "--attention", "--lang", botconfig.load().get("lang", "ru"),
                    "--telegram"]
            send("⏳ Быстрый прогон (топ-20, ~1–2 мин)…")
        else:
            cfg = botconfig.load()
            argv = [sys.executable, os.path.join(HERE, "scout.py")] + botconfig.to_argv(cfg) + ["--log", "--telegram"]
            send(f"⏳ Полный прогон (топ-{cfg.get('top', 100)}, ~3–5 мин). Для быстрого — /quick")
        r = subprocess.run(argv, cwd=HERE, timeout=900, capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["неизвестная ошибка"]
            hint = ""
            if "429" in (r.stderr or ""):
                hint = "\nПохоже на лимит запросов к API (CoinGecko). Подожди минуту и снова /run."
            send(f"⚠️ Прогон не завершился: {tail[0][:200]}{hint}")
    except subprocess.TimeoutExpired:
        send("⚠️ Прогон не уложился в 15 минут и был прерван.")
    except Exception as e:
        send(f"⚠️ Прогон упал: {e}")
    finally:
        _run_lock.release()


def settings_text(cfg):
    on = lambda b: "✅" if b else "❌"
    return (f"<b>⚙️ Настройки:</b>\n"
            f"монет: <b>{cfg['top']}</b> (/top N)\n"
            f"язык новостей: <b>{cfg['lang']}</b> (/lang ru|en)\n"
            f"{on(cfg['ta'])} осцилляторы (ta)\n"
            f"{on(cfg['fng'])} фон рынка (fng)\n"
            f"{on(cfg['scalp'])} фандинг/скальп (scalp)\n"
            f"{on(cfg['listings'])} листинги (listings)\n"
            f"{on(cfg['news'])} новости Jev (news)\n"
            f"{on(cfg['attention'])} подсказки «на что смотреть» (attention)\n\n"
            f"Вкл/выкл: /on scalp · /off listings")


def handle(text):
    cfg = botconfig.load()
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/").split("@")[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("start", "help"):
        send(HELP)
    elif cmd == "legend":
        send(LEGEND)
    elif cmd == "settings":
        send(settings_text(cfg))
    elif cmd == "run":
        threading.Thread(target=run_scout, daemon=True).start()
    elif cmd == "quick":
        threading.Thread(target=lambda: run_scout(quick=True), daemon=True).start()
    elif cmd == "top":
        if arg.isdigit() and 1 <= int(arg) <= 250:
            cfg["top"] = int(arg); botconfig.save(cfg)
            send(f"✅ Теперь отслеживаю топ-{arg} монет.")
        else:
            send("Формат: /top 50 (число 1–250)")
    elif cmd == "lang":
        if arg in ("ru", "en"):
            cfg["lang"] = arg; botconfig.save(cfg)
            send(f"✅ Язык новостей: {arg}")
        else:
            send("Формат: /lang ru или /lang en")
    elif cmd in ("on", "off"):
        if arg in botconfig.BOOL_KEYS:
            cfg[arg] = (cmd == "on"); botconfig.save(cfg)
            send(f"✅ {arg}: {'включено' if cmd == 'on' else 'выключено'}")
        else:
            send(f"Блоки: {', '.join(botconfig.BOOL_KEYS)}\nПример: /on scalp")
    else:
        send("Не понял. /help — список команд.")


def main():
    if not TOKEN or not OWNER:
        sys.exit("нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    send("🤖 Бот запущен. /help — что умею.")
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = api("getUpdates", **params)
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                chat = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")
                print(f"update from chat={chat} (owner={OWNER}) text={text!r}", file=sys.stderr, flush=True)
                if chat != OWNER or not text:
                    continue  # только владелец
                try:
                    handle(text)
                except Exception as e:
                    print(f"handle error: {e!r}", file=sys.stderr, flush=True)
                    try:
                        send(f"⚠️ Ошибка: {e}")
                    except Exception:
                        pass
        except Exception as e:
            print(f"poll error: {e!r}", file=sys.stderr, flush=True)
            time.sleep(5)


def selftest():
    # settings_text and to_argv coherence without network
    cfg = botconfig.load()
    t = settings_text(cfg)
    assert "Настройки" in t and str(cfg["top"]) in t
    assert "✅" in t or "❌" in t
    assert "regulation" in HELP and "oversold" in HELP  # help explains the tags
    print("bot selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()

#!/usr/bin/env python3
"""Interactive Telegram bot — long-polling, no dependency (plain Bot API).

A persistent reply-keyboard menu (like polyradar) for the common actions, and
inline screens that edit themselves in place: settings toggles, the stop
comparison with horizon buttons, the demo account, the public channel.
Runs as a long-lived systemd service alongside the scheduled timer; both read
the same config.json (botconfig.py), so a change here applies to the next
scheduled run too.

Only the chat in TELEGRAM_CHAT_ID is honored — commands from anyone else are
ignored. Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / CHANNEL_ID and the Jev
key from the environment (loaded from .env by the service).
"""
import html, os, subprocess, sys, threading, time

import botconfig
import telegram_notify as tn

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
CHANNEL = os.environ.get("CHANNEL_ID", "")

MENU = [["⚡ Быстро", "📊 Полный прогон"],
        ["💼 Демо-счёт", "⚖️ Стопы"],
        ["📈 Самопроверка", "⚙️ Настройки"],
        ["🐋 Киты", "📡 Канал"],
        ["❓ Помощь"]]
MENU_ACTIONS = dict(zip([b for row in MENU for b in row],
                        ["quick", "run", "paper", "compare", "stats", "settings", "whales", "channel", "help"]))
KEYBOARD = {"keyboard": [[{"text": b} for b in row] for row in MENU],
            "resize_keyboard": True, "is_persistent": True}

TOGGLES = [("ta", "осцилляторы"), ("scalp", "фандинг"), ("fng", "фон рынка"),
           ("listings", "листинги"), ("news", "новости Jev"), ("attention", "куда смотреть"),
           ("radar", "фандинг-радар")]
TOP_CHOICES = [20, 50, 100, 150]
HORIZONS = [(24, "24ч"), (48, "48ч"), (168, "7д")]

HELP = """<b>🤖 Jev Crypto Scout</b>

Скринер крипты: индикаторы считает код, новости разбирает ИИ-модель Jev. Ничего не покупает и не продаёт — присылает сводку, решаешь ты.

<b>Меню внизу:</b>
⚡ <b>Быстро</b> — топ-20 без осцилляторов, ~1–2 мин
📊 <b>Полный прогон</b> — по настройкам, ~3–5 мин
💼 <b>Демо-счёт</b> — вырос бы виртуальный $10 000 по правилу (стопы по реальным свечам, с комиссиями)
⚖️ <b>Стопы</b> — какой стоп лучше: 2/3/5% на тех же сделках, горизонт 24ч/48ч/7д
🧪 /rules — все правила входа на одном движке (RSI, 🎯 фандинг+перегиб, против толпы) со значимостью
📈 <b>Самопроверка</b> — есть ли у сигналов эдж против случайной монеты (со значимостью)
⚙️ <b>Настройки</b> — сколько монет, язык, какие блоки (кнопками)
🐋 <b>Киты</b> — где сейчас стоят топ-трейдеры Hyperliquid и их последние крупные входы/выходы (алерты приходят сами)
📡 <b>Канал</b> — публичный канал: превью, публикация, счёт

/legend — что значат все значки в сводке
Команды тоже работают: /run /quick /paper /compare 48 /stats /top 50 /lang en /on scalp /off news

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
ставка +0.01% — реальная ставка (как на бирже Bybit)
z=+2.1 — насколько ставка аномальна vs её истории (НЕ проценты! z≠ставка)
🎯 — редкий двойной сигнал (фандинг + осциллятор)
🧲 радар — самые большие ставки по ВСЕМ монетам Bybit: ставка за период (1ч/4ч/8ч) и в годовых. Норма ≈ 0.01%/8ч ≈ 11% годовых; 0.1%/8ч ≈ 110% годовых — уже много

<b>Новости:</b>
🟢 хорошо для цены / 🔴 плохо
[regulation] [hack_exploit] [listing] [partnership] [hype_speculation] — тип события

<b>Биржа:</b>
📈 новый листинг (свежая ликвидность/внимание)
📉 делистинг (риск)

<b>Настроение рынка:</b>
🌡 Fear&amp;Greed 0–100 (страх ↔ жадность)
DeFi TVL — сколько денег заблокировано в DeFi"""


# ---------- Telegram ----------

def send(text, kb=None):
    if kb is None:
        return tn.send_text(text)  # длинное режется на части
    return tn._post(text, reply_markup=kb)


def edit(msg_id, text, kb=None):
    """Edit in place; 'message is not modified' (same button twice) is fine."""
    try:
        tn.call("editMessageText", chat_id=OWNER, message_id=msg_id, text=text, parse_mode="HTML",
                disable_web_page_preview="true", reply_markup=kb)
    except RuntimeError as e:
        if "not modified" not in str(e):
            raise


def ikb(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}


def btn(text, data):
    return {"text": text, "callback_data": data}


# ---------- прогоны ----------

_run_lock = threading.Lock()


def run_scout(quick=False, extra=(), telegram=True, log=True):
    """Trigger a run in a thread so polling keeps responding; scout.py sends
    the digest/post itself. Reports failure instead of dying silently."""
    if not _run_lock.acquire(blocking=False):
        send("⏳ Прогон уже идёт, подожди его окончания.")
        return
    try:
        cfg = botconfig.load()
        if quick:
            # fast: skip --ta (sequential CoinGecko OHLC is the slow part)
            argv = ["--top", "20", "--fng", "--scalp", "--listings", "--news", "--attention",
                    "--lang", cfg.get("lang", "ru")]
            send("⏳ Быстрый прогон (топ-20, ~1–2 мин)…")
        else:
            argv = botconfig.to_argv(cfg)
            send(f"⏳ Полный прогон (топ-{cfg.get('top', 100)}, ~3–5 мин)…")
        argv += (["--log"] if log else []) + (["--telegram"] if telegram else []) + list(extra)
        r = subprocess.run([sys.executable, os.path.join(HERE, "scout.py")] + argv,
                           cwd=HERE, timeout=900, capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["неизвестная ошибка"]
            hint = "\nПохоже на лимит CoinGecko. Подожди минуту и повтори." if "429" in (r.stderr or "") else ""
            send(f"⚠️ Прогон не завершился: {html.escape(tail[0][:300])}{hint}")
        elif "--channel" in extra:
            send(f"✅ Пост опубликован в {html.escape(CHANNEL)}")
    except subprocess.TimeoutExpired:
        send("⚠️ Прогон не уложился в 15 минут и был прерван.")
    except Exception as e:
        send(f"⚠️ Прогон упал: {e}")
    finally:
        _run_lock.release()


def bg(fn, *a, **kw):
    threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()


# ---------- реальные свечи для честных стопов (кэш на час) ----------

_paths = {"ts": 0, "data": None}
_paths_lock = threading.Lock()


def paths():
    """Bybit 1h paths for every flagged coin; built with the 7d horizon so the
    one cache serves 24ч/48ч/7д. ~20–40с cold, instant for the next hour."""
    import paper
    with _paths_lock:
        if not _paths["data"] or time.time() - _paths["ts"] > 3600:
            conn = paper.history.connect()
            _paths["data"] = paper.build_paths(conn, 168)
            _paths["ts"] = time.time()
            conn.close()
        return _paths["data"]


# ---------- экраны ----------

def settings_view(cfg):
    on = lambda k: "✅" if cfg.get(k) else "▫️"
    text = (f"<b>⚙️ Настройки</b>\n"
            f"Монет: <b>{cfg['top']}</b> · новости: <b>{cfg['lang']}</b>\n"
            f"Меняется сразу и для прогонов по расписанию (каждые 4ч).")
    rows = [[btn(("● " if cfg["top"] == n else "") + str(n), f"top:{n}") for n in TOP_CHOICES]]
    for i in range(0, len(TOGGLES), 2):
        rows.append([btn(f"{on(k)} {label}", f"tg:{k}") for k, label in TOGGLES[i:i + 2]])
    rows.append([btn(("● " if cfg["lang"] == l else "") + name, f"lang:{l}")
                 for l, name in (("ru", "🇷🇺 новости RU"), ("en", "🇬🇧 новости EN"))])
    return text, ikb(*rows)


def horizon_row(prefix, active):
    return [btn(("● " if h == active else "") + label, f"{prefix}:{h}") for h, label in HORIZONS]


def compare_screen(h, msg_id=None):
    import paper
    if msg_id is None:
        msg_id = send("⏳ Считаю стопы по реальным свечам Bybit…", ikb())["message_id"]
    else:
        edit(msg_id, "⏳ Считаю стопы по реальным свечам Bybit…")
    try:
        conn = paper.history.connect()
        rows = paper.compare_stops(conn, h, paths=paths())
        conn.close()
        edit(msg_id, paper.compare_text(rows, h, True), ikb(horizon_row("cmp", h),
                                                              [btn("💼 Демо-счёт", "paper:24"), btn("🧪 Правила", f"rules:{h}")]))
    except Exception as e:
        edit(msg_id, f"⚠️ Не удалось посчитать: {html.escape(str(e))}", ikb(horizon_row("cmp", h)))


def paper_screen(h, msg_id=None):
    import paper
    if msg_id is None:
        msg_id = send("⏳ Считаю демо-счёт по реальным свечам…", ikb())["message_id"]
    else:
        edit(msg_id, "⏳ Считаю демо-счёт по реальным свечам…")
    try:
        conn = paper.history.connect()
        r = paper.simulate(conn, h, paths=paths())
        conn.close()
        edit(msg_id, paper.summary_text(r), ikb(horizon_row("paper", h), [btn("⚖️ Стопы", f"cmp:{h}"), btn("🧪 Правила", f"rules:{h}")]))
    except Exception as e:
        edit(msg_id, f"⚠️ Не удалось посчитать демо-счёт: {html.escape(str(e))}", ikb(horizon_row("paper", h)))


def rules_screen(h, msg_id=None):
    import paper
    if msg_id is None:
        msg_id = send("⏳ Гоняю все правила на бумаге…", ikb())["message_id"]
    else:
        edit(msg_id, "⏳ Гоняю все правила на бумаге…")
    try:
        conn = paper.history.connect()
        rows = paper.compare_rules(conn, h, paths=paths())
        conn.close()
        edit(msg_id, paper.rules_text(rows, h), ikb(horizon_row("rules", h), [btn("⚖️ Стопы", f"cmp:{h}")]))
    except Exception as e:
        edit(msg_id, f"⚠️ Не удалось посчитать: {html.escape(str(e))}", ikb(horizon_row("rules", h)))


def channel_view():
    import channel
    lines = ["<b>📡 Публичный канал</b>",
             "Каждое утро в 07:00 МСК: флаги дня + вердикт по вчерашним ответом на вчерашний пост, "
             "с минусами. Счёт канала копится с первого поста.\n"]
    if CHANNEL:
        lines.append(f"Канал: <b>{html.escape(CHANNEL)}</b> ✅")
    else:
        lines.append("Канал: <b>не подключён</b> ❌\nСоздай канал, добавь бота админом и пропиши "
                     "<code>CHANNEL_ID=@имя</code> в .env на сервере. Превью работает и без канала.")
    try:
        conn = channel.connect()
        last = channel.last_post(conn)
        w, l, tot = channel.record(conn)
        conn.close()
        if last:
            lines.append(f"Последний пост: {time.strftime('%d.%m %H:%M UTC', time.gmtime(last[0]))}")
        if w + l:
            lines.append(f"Счёт канала: <b>{w}✅ / {l}❌</b> · сумма {tot:+.1f}%")
    except Exception:
        pass
    rows = [[btn("👁 Превью поста (~4 мин)", "ch:preview")]]
    if CHANNEL:
        rows.append([btn("📤 Опубликовать сейчас", "ch:publish")])
    return "\n".join(lines), ikb(*rows)


def stats_text():
    import validate
    conn = validate.history.connect()
    total = conn.execute("SELECT COUNT(*) FROM scout_runs").fetchone()[0]
    out = (validate.summary_text(validate.compute(conn, 24)) if total
           else "📈 История пуста. Прогоны копятся автоматически — загляни через день-два.")
    conn.close()
    return out


# ---------- обработка ----------

def handle(text):
    cfg = botconfig.load()
    parts = text.strip().split()
    cmd = MENU_ACTIONS.get(text.strip()) or (parts[0].lower().lstrip("/").split("@")[0] if parts else "")
    arg = parts[1] if len(parts) > 1 and text.strip() not in MENU_ACTIONS else ""

    if cmd == "start":
        send(HELP, KEYBOARD)
    elif cmd == "help":
        send(HELP, KEYBOARD)
    elif cmd == "legend":
        send(LEGEND)
    elif cmd == "stats":
        send(stats_text())
    elif cmd == "paper":
        bg(paper_screen, int(arg) if arg.isdigit() else 24)
    elif cmd == "rules":
        bg(rules_screen, int(arg) if arg.isdigit() else 24)
    elif cmd == "compare":
        bg(compare_screen, int(arg) if arg.isdigit() else 24)
    elif cmd == "settings":
        send(*settings_view(cfg))
    elif cmd == "whales":
        import whales
        conn = whales.connect()
        send(whales.summary_text(conn))
        conn.close()
    elif cmd == "channel":
        send(*channel_view())
    elif cmd == "run":
        bg(run_scout)
    elif cmd == "quick":
        bg(run_scout, quick=True)
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
        send("Не понял. Меню внизу или /help.", KEYBOARD)


def on_callback(q):
    data, msg_id = q.get("data", ""), (q.get("message") or {}).get("message_id")
    kind, _, val = data.partition(":")
    toast = ""
    cfg = botconfig.load()
    if kind == "top" and val.isdigit():
        cfg["top"] = int(val); botconfig.save(cfg)
        edit(msg_id, *settings_view(cfg))
    elif kind == "tg" and val in botconfig.BOOL_KEYS:
        cfg[val] = not cfg.get(val); botconfig.save(cfg)
        edit(msg_id, *settings_view(cfg))
    elif kind == "lang" and val in ("ru", "en"):
        cfg["lang"] = val; botconfig.save(cfg)
        edit(msg_id, *settings_view(cfg))
    elif kind == "cmp" and val.isdigit():
        bg(compare_screen, int(val), msg_id)
    elif kind == "paper" and val.isdigit():
        bg(paper_screen, int(val), msg_id)
    elif kind == "rules" and val.isdigit():
        bg(rules_screen, int(val), msg_id)
    elif data == "ch:preview":
        toast = "Готовлю превью — придёт сюда через ~4 мин"
        bg(run_scout, extra=["--channel-preview"], telegram=False, log=False)
    elif data == "ch:publish" and CHANNEL:
        toast = "Публикую — полный прогон ~4 мин"
        bg(run_scout, extra=["--channel"])
    tn.call("answerCallbackQuery", callback_query_id=q["id"], text=toast[:190])


def main():
    if not TOKEN or not OWNER:
        sys.exit("нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    tn.call("setMyCommands", commands=[
        {"command": "start", "description": "меню"},
        {"command": "quick", "description": "быстрая сводка"},
        {"command": "paper", "description": "демо-счёт"},
        {"command": "compare", "description": "сравнить стопы"},
        {"command": "rules", "description": "сравнить правила входа"},
        {"command": "whales", "description": "киты Hyperliquid"},
        {"command": "legend", "description": "что значат значки"}])
    send("🤖 Бот перезапущен. Меню внизу 👇", KEYBOARD)
    offset = None
    while True:
        try:
            ups = tn.call("getUpdates", _timeout=45, timeout=30, offset=offset,
                          allowed_updates=["message", "callback_query"])
            for u in ups:
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        q = u["callback_query"]
                        if str(q["from"]["id"]) == OWNER:
                            on_callback(q)
                        continue
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id", "")) == OWNER and msg.get("text"):
                        handle(msg["text"])
                except Exception as e:
                    print(f"handle error: {e!r}", file=sys.stderr, flush=True)
                    try:
                        send(f"⚠️ Ошибка: {html.escape(str(e))}")
                    except Exception:
                        pass
        except Exception as e:
            print(f"poll error: {e!r}", file=sys.stderr, flush=True)
            time.sleep(5)


def selftest():
    # every menu label routes to a real command
    assert set(MENU_ACTIONS.values()) == {"quick", "run", "paper", "compare", "stats", "settings", "whales", "channel", "help"}
    cfg = dict(botconfig.DEFAULTS, top=50, scalp=False)
    text, kb = settings_view(cfg)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert "Настройки" in text and "50" in text
    assert any(b["text"] == "● 50" for b in flat), "active top is marked"
    assert any(b["callback_data"] == "tg:scalp" and b["text"].startswith("▫️") for b in flat), "off toggle shown off"
    assert all(len(b["callback_data"]) <= 64 for b in flat)  # Telegram limit
    assert [b["callback_data"] for b in horizon_row("cmp", 48)] == ["cmp:24", "cmp:48", "cmp:168"]
    assert "regulation" in LEGEND and "Стопы" in HELP
    print("bot selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()

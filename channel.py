"""Public channel — «журнал, который публикует свои минусы».

One post a day (the morning scheduled run), built from the live run's data:

  1. Вердикт по вчерашним флагам — WITH the losses, judged by the same rules as
     the demo account (3% stop checked on real Bybit candles, fees+slippage).
     The post is sent as a REPLY to yesterday's post, so in the channel feed
     every call is chained to its outcome: you can't quietly drop a bad day.
  2. Флаги дня — the fixed mean-reversion rule calls its bets publicly BEFORE
     the move (oversold → long, overbought → short). Verdict comes tomorrow.
  3. Счёт канала — all-time record of the channel's own calls, since post #1.
  4. Термометр толпы — where funding is abnormal (the crowd overpays).
  5. Jev: куда смотреть — attention, not buy/sell.
  6. Демо-счёт правила — the whole rule, every trade since day one.

Why: crypto channels show only wins. This one shows everything, which is the
product (trust); the bot/hosting is where the button leads.

    scout.py ... --channel           # post to CHANNEL_ID, record the picks
    scout.py ... --channel-preview   # same post to the owner chat, no side effects
"""
import html, json, os, time

import history
import paper
import telegram_notify

REPO_URL = "https://github.com/yasdelayu/jev-crypto-scout"
MAX_PICKS = 5
RESOLVE_AFTER_S = 20 * 3600  # вчерашний пост судим, если ему ≥20ч (таймер раз в сутки ± сдвиг)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, chat TEXT, message_id INTEGER,
    picks TEXT NOT NULL,          -- [{"symbol","dir","price","rsi"}]
    results TEXT                  -- NULL пока не судили; [{"symbol","dir","net","stopped"}]
);
"""


def connect(path=None):
    conn = history.connect(path)
    conn.executescript(SCHEMA)
    return conn


def pick_flags(ranked, n=MAX_PICKS):
    """Today's public bets: the most extreme RSI flags, same rule as paper.py."""
    flagged = [c for c in ranked
               if (c.get("osc") or {}).get("signal") in ("oversold", "overbought") and c.get("current_price")]
    flagged.sort(key=lambda c: abs(((c.get("osc") or {}).get("rsi") or 50) - 50), reverse=True)
    return [{"symbol": c["symbol"].upper(),
             "dir": "long" if c["osc"]["signal"] == "oversold" else "short",
             "price": c["current_price"], "rsi": c["osc"].get("rsi")} for c in flagged[:n]]


def judge(pick, entry_ts, exit_price, now_ts, path=None):
    """One call's outcome, by the demo account's rules: stop on the real path,
    net of round-trip costs. None if we have no exit price for the coin."""
    if not exit_price:
        return None
    d = 1 if pick["dir"] == "long" else -1
    raw = d * (exit_price - pick["price"]) / pick["price"]
    hit = paper._stopped_intrabar(path, entry_ts, now_ts, pick["price"], d, paper.STOP_PCT)
    if hit is None:
        hit = raw <= -paper.STOP_PCT
    net = (-paper.STOP_PCT if hit else raw) - paper.COST_PCT
    return {"symbol": pick["symbol"], "dir": pick["dir"], "net": round(net * 100, 2), "stopped": hit}


def resolve(conn, ranked, now_ts, fetch=True):
    """Judge every unresolved post older than RESOLVE_AFTER_S. Returns
    [(post_row_id, message_id, results)] — nothing is written here."""
    prices = {c["symbol"].upper(): c.get("current_price") for c in ranked}
    out = []
    for pid, ts, mid, picks in conn.execute(
            "SELECT id, ts, message_id, picks FROM channel_posts WHERE results IS NULL AND ts <= ? ORDER BY ts",
            (now_ts - RESOLVE_AFTER_S,)).fetchall():
        res = []
        for p in json.loads(picks):
            path = paper._fetch_lowhigh(p["symbol"] + "USDT", (ts - 3600) * 1000, now_ts * 1000) if fetch else None
            r = judge(p, ts, prices.get(p["symbol"]), now_ts, path)
            res.append(r or {"symbol": p["symbol"], "dir": p["dir"], "net": None, "stopped": False})
        out.append((pid, mid, res))
    return out


def record(conn):
    """All-time channel scoreboard over judged calls: (wins, losses, sum net %)."""
    wins = losses = 0
    total = 0.0
    for (res,) in conn.execute("SELECT results FROM channel_posts WHERE results IS NOT NULL"):
        for r in json.loads(res):
            if r.get("net") is None:
                continue
            total += r["net"]
            wins += r["net"] > 0
            losses += r["net"] <= 0
    return wins, losses, round(total, 2)


def _dir_ru(d):
    return "лонг" if d == "long" else "шорт"


def format_post(date, context, verdicts, picks, rec, scalp_results, priorities, acct):
    e = html.escape
    lines = [f"📡 <b>Jev Scout · {date}</b>"]
    if context and context.get("fear_greed"):
        fg = context["fear_greed"]
        lines.append(f"🌡 Fear&amp;Greed <b>{fg['value']}</b> · {e(fg['label'])}")

    judged = [r for _, _, res in verdicts for r in res]
    if judged:
        lines.append("\n📒 <b>Вердикт по вчерашним флагам</b>")
        nets = []
        for r in judged:
            if r["net"] is None:
                lines.append(f"➖ {e(r['symbol'])} {_dir_ru(r['dir'])} — нет цены (выпала из топа)")
                continue
            nets.append(r["net"])
            mark = "✅" if r["net"] > 0 else "❌"
            stop = " · стоп" if r["stopped"] else ""
            lines.append(f"{mark} {e(r['symbol'])} {_dir_ru(r['dir'])} <b>{r['net']:+.1f}%</b>{stop}")
        if nets:
            lines.append(f"<i>итог дня {sum(nets):+.1f}% · после комиссий, стоп 3% по реальным свечам</i>")

    w, l, tot = rec
    if w + l:
        lines.append(f"🧾 Счёт канала: <b>{w}✅ / {l}❌</b> · сумма {tot:+.1f}%")

    lines.append("\n🧪 <b>Флаги дня</b> — публичный эксперимент: правило ставит против перегиба")
    if acct and acct["trades"] and acct["pnl_pct"] < 0:
        lines.append(f"<i>⚠️ правило сейчас в минусе ({acct['pnl_pct']:+.1f}% на демо) — не повторяй сделки, "
                     f"мы проверяем, работает ли оно, а не советуем</i>")
    if picks:
        for p in picks:
            icon = "🟢" if p["dir"] == "long" else "🔴"
            why = "перепродан" if p["dir"] == "long" else "перекуплен"
            rsi = f"RSI {p['rsi']:.0f} · " if p.get("rsi") is not None else ""
            lines.append(f"{icon} <b>{e(p['symbol'])}</b> {_dir_ru(p['dir'])} · {rsi}{why} · ${p['price']:,.4g}")
        lines.append("<i>Вердикт — завтра, ответом на этот пост. Стоп 3%, горизонт сутки.</i>")
    else:
        lines.append("сегодня перегибов нет — правило молчит (это тоже результат)")

    skew = sorted([r for r in (scalp_results or []) if "error" not in r and r.get("funding", {}).get("z") is not None],
                  key=lambda r: abs(r["funding"]["z"]), reverse=True)[:3]
    skew = [r for r in skew if abs(r["funding"]["z"]) >= 2]
    if skew:
        lines.append("\n🌡 <b>Термометр толпы</b> (фандинг):")
        for r in skew:
            f = r["funding"]
            who = "лонги переплачивают" if f["current"] > 0 else "шорты переплачивают"
            lines.append(f"{e(r['symbol'].replace('USDT', ''))} — {who} {f['current']*100:+.3f}% "
                         f"<i>(аномалия z={f['z']:+.1f})</i>")

    worth = [p for p in (priorities or []) if p["attention"] >= 1.0][:3]
    if worth:
        import attention
        lines.append("\n🎯 <b>Jev: куда смотреть</b>")
        for p in worth:
            lines.append(f"{e(p['symbol'])} — {attention.ACTION_LABEL.get(p['action'], p['action'])}")

    if acct and acct["trades"]:
        lines.append(f"\n💼 <b>Демо-счёт правила</b>: $10 000 → <b>${acct['balance']:,.0f}</b> "
                     f"({acct['pnl_pct']:+.1f}%) · {acct['trades']} сделок, все с первого дня")

    lines.append("\n<i>Не инвестсовет. Публикуем всё, включая минусы.</i>")
    return "\n".join(lines)


def _bot_username():
    try:
        return telegram_notify.call("getMe")["username"]
    except Exception:
        return None


def post(ranked, scalp_results=None, context=None, priorities=None, preview=False, now_ts=None):
    """Build and send the daily post. preview=True → owner chat, nothing saved."""
    now_ts = now_ts or int(time.time())
    target = os.environ.get("TELEGRAM_CHAT_ID") if preview else os.environ.get("CHANNEL_ID")
    if not target:
        raise RuntimeError("нет CHANNEL_ID в .env (канал, где бот — админ)")
    conn = connect()
    last = last_post(conn)
    if not preview and last and now_ts - last[0] < 12 * 3600:
        conn.close()
        raise RuntimeError(f"пост уже был {time.strftime('%d.%m %H:%M UTC', time.gmtime(last[0]))} — один в день")
    verdicts = resolve(conn, ranked, now_ts)
    picks = pick_flags(ranked)
    acct = paper.simulate(conn, 24, paths=paper.build_paths(conn, 24))

    # счёт канала включает сегодняшние вердикты, даже в превью (не сохраняя их)
    w, l, tot = record(conn)
    for _, _, res in verdicts:
        for r in res:
            if r["net"] is not None:
                w, l, tot = w + (r["net"] > 0), l + (r["net"] <= 0), round(tot + r["net"], 2)

    text = format_post(time.strftime("%d.%m", time.gmtime(now_ts)), context, verdicts, picks,
                       (w, l, tot), scalp_results, priorities, acct)
    if preview:
        text = "👁 <b>ПРЕВЬЮ поста канала</b> (не опубликовано)\n\n" + text
    bot = _bot_username()
    kb = {"inline_keyboard": [[b for b in (
        {"text": "🤖 Бот", "url": f"https://t.me/{bot}?start=ch"} if bot else None,
        {"text": "📂 Код и методика", "url": REPO_URL}) if b]]}
    reply_to = next((mid for _, mid, _ in reversed(verdicts) if mid), None)
    extra = {"reply_markup": kb}
    if reply_to and not preview:
        extra["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    msg = telegram_notify._post(text[:4096], chat_id=target, **extra)

    if not preview:
        for pid, _, res in verdicts:
            conn.execute("UPDATE channel_posts SET results=? WHERE id=?", (json.dumps(res), pid))
        conn.execute("INSERT INTO channel_posts (ts, chat, message_id, picks) VALUES (?,?,?,?)",
                     (now_ts, str(target), msg["message_id"], json.dumps(picks)))
        conn.commit()
    conn.close()
    return msg


def last_post(conn=None):
    """(ts, message_id) of the latest real post, for the bot's channel screen."""
    own = conn is None
    conn = conn or connect()
    row = conn.execute("SELECT ts, message_id FROM channel_posts ORDER BY ts DESC LIMIT 1").fetchone()
    if own:
        conn.close()
    return row


def selftest():
    conn = connect(":memory:")
    now = int(time.time())
    ranked = [{"symbol": "sol", "current_price": 100.0, "osc": {"signal": "oversold", "rsi": 22}},
              {"symbol": "xrp", "current_price": 1.0, "osc": {"signal": "overbought", "rsi": 74}},
              {"symbol": "btc", "current_price": 50000.0, "osc": {"signal": None, "rsi": 50}}]
    picks = pick_flags(ranked)
    assert [p["symbol"] for p in picks] == ["SOL", "XRP"], picks  # most extreme RSI first, neutral excluded
    assert picks[0]["dir"] == "long" and picks[1]["dir"] == "short"

    # yesterday's post: SOL long @100, XRP short @1.0
    conn.execute("INSERT INTO channel_posts (ts, chat, message_id, picks) VALUES (?,?,?,?)",
                 (now - 86400, "@x", 42, json.dumps(picks)))
    conn.commit()
    today = [{"symbol": "sol", "current_price": 105.0}, {"symbol": "xrp", "current_price": 1.02}]
    v = resolve(conn, today, now, fetch=False)
    assert len(v) == 1 and v[0][1] == 42, v  # replies to message 42
    res = {r["symbol"]: r for r in v[0][2]}
    assert res["SOL"]["net"] == round((0.05 - paper.COST_PCT) * 100, 2) and not res["SOL"]["stopped"]
    assert res["XRP"]["net"] < 0  # short into a +2% rise loses — and it's SHOWN
    # a post younger than 20h is not judged yet
    conn.execute("INSERT INTO channel_posts (ts, chat, message_id, picks) VALUES (?,?,?,?)",
                 (now - 3600, "@x", 43, json.dumps(picks)))
    assert len(resolve(conn, today, now, fetch=False)) == 1

    # intrabar: long that ends green but dipped past 3% → stopped, loss shown
    path = [((now - 86400 - 1800) * 1000, 100.5, 99.5), ((now - 80000) * 1000, 101.0, 96.0)]
    j = judge({"symbol": "SOL", "dir": "long", "price": 100.0}, now - 86400, 105.0, now, path)
    assert j["stopped"] and j["net"] == round((-paper.STOP_PCT - paper.COST_PCT) * 100, 2), j

    conn.execute("UPDATE channel_posts SET results=? WHERE id=1", (json.dumps(v[0][2]),))
    assert record(conn)[:2] == (1, 1)

    txt = format_post("28.09", {"fear_greed": {"value": 34, "label": "Fear"}}, v, picks, (1, 1, 2.0),
                      [{"symbol": "ENAUSDT", "funding": {"z": -9.5, "current": -0.0005}}],
                      [{"symbol": "ENA", "attention": 2.0, "action": "research"}],
                      {"trades": 10, "balance": 10500, "pnl_pct": 5.0})
    for s in ("Вердикт", "❌ XRP", "✅ SOL", "Флаги дня", "Счёт канала", "Термометр", "шорты переплачивают",
              "куда смотреть", "Демо-счёт", "включая минусы"):
        assert s in txt, s
    assert len(txt) < 4096
    empty = format_post("28.09", None, [], [], (0, 0, 0), None, None, None)
    assert "правило молчит" in empty
    losing = format_post("28.09", None, [], picks, (0, 0, 0), None, None, {"trades": 5, "balance": 9000, "pnl_pct": -10.0})
    assert "в минусе" in losing and "не повторяй" in losing  # a losing rule is labelled as such next to its flags
    print("channel selftest ok")


if __name__ == "__main__":
    selftest()

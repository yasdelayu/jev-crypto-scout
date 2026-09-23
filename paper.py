"""Paper-trading simulator over accumulated history — the honest 'demo account'.

It does NOT touch any exchange, hold any key, or place any order (not even on
a testnet). It replays a FIXED, transparent rule against the prices already in
history.db and tracks a virtual balance, so you can watch whether following
that rule would have grown or shrunk a demo account — forward, on real prices,
without risking a cent. This is the step the tweet skipped straight past.

The rule is deliberately plain and lives in code, not in Jev — mean reversion
on RSI: a coin flagged `oversold` is bought (bet on a bounce up), `overbought`
is shorted (bet on a pullback), each closed after `horizon_h` hours at the
price then. Jev never says buy/sell here; it's a code rule being measured.
Swap the rule and re-run to test another idea — that's the whole point.

    python3 paper.py                 # report the virtual account
    python3 paper.py --json          # machine summary (bot /paper uses it)
"""
import argparse
import history

START_BALANCE = 10_000.0   # виртуальные $
RISK_PCT = 0.01            # РИСК на сделку: 1% капитала теряется, ЕСЛИ сработал стоп
STOP_PCT = 0.03           # стоп-лосс: 3% против позиции от входа
# Размер позиции вычисляется из этих двух (риск = размер × расстояние_до_стопа),
# а НЕ «X% капитала». Это тот самый урок из видео Standard Deviation: риск —
# это сколько теряешь при стопе, не сколько актива покупаешь. Так потеря по
# любой сделке ограничена RISK_PCT капитала, а размер подстраивается под стоп.


def _price_after(conn, symbol, after_ts, horizon_s):
    row = conn.execute(
        "SELECT ts, price FROM scout_runs WHERE symbol=? AND ts>=? AND price IS NOT NULL ORDER BY ts ASC LIMIT 1",
        (symbol, after_ts + horizon_s)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def simulate(conn, horizon_h=24):
    """Replay the mean-reversion rule with PROPER risk management: each trade
    risks a fixed RISK_PCT of capital, capped by a STOP_PCT stop-loss. Position
    size follows from that, not the other way round. A trade closes at the stop
    (loss = RISK_PCT) if price moved STOP_PCT against it by the horizon,
    otherwise at the horizon price.

    We only have prices at log points (every ~4h), not ticks, so 'stop hit'
    is checked at the horizon, not intrabar — an approximation, stated plainly,
    but far honester than the old 'N% of capital per trade'."""
    rows = conn.execute(
        "SELECT symbol, ts, price, signal FROM scout_runs "
        "WHERE signal IN ('oversold','overbought') AND price IS NOT NULL ORDER BY ts ASC").fetchall()
    balance = START_BALANCE
    trades, wins = [], 0
    for symbol, ts, entry, signal in rows:
        exit_ts, exit_price = _price_after(conn, symbol, ts, horizon_h * 3600)
        if not exit_price:
            continue  # позиция ещё «открыта» — нет цены выхода в истории
        direction = 1 if signal == "oversold" else -1  # long / short
        raw_ret = direction * (exit_price - entry) / entry
        # риск-$ фиксирован; размер такой, что движение на STOP_PCT = потеря RISK_PCT
        risk_dollars = balance * RISK_PCT
        if raw_ret <= -STOP_PCT:
            pnl = -risk_dollars                       # стоп: теряем ровно заложенный риск
            realized_ret = -STOP_PCT
        else:
            pnl = risk_dollars * (raw_ret / STOP_PCT)  # прибыль/убыток в единицах риска (R)
            realized_ret = raw_ret
        balance += pnl
        wins += realized_ret > 0
        trades.append({"symbol": symbol, "signal": signal, "dir": "long" if direction > 0 else "short",
                       "entry": entry, "exit": exit_price, "ret_pct": realized_ret * 100,
                       "pnl": pnl, "stopped": raw_ret <= -STOP_PCT})
    n = len(trades)
    return {
        "horizon_h": horizon_h, "trades": n,
        "start": START_BALANCE, "balance": round(balance, 2),
        "pnl": round(balance - START_BALANCE, 2),
        "pnl_pct": round((balance / START_BALANCE - 1) * 100, 2),
        "winrate": round(wins / n * 100, 1) if n else None,
        "stops": sum(1 for t in trades if t["stopped"]),
        "risk_pct": RISK_PCT, "stop_pct": STOP_PCT,
        "closed": trades,
    }


def report(r):
    print(f"<Демо-счёт> mean-reversion по RSI, горизонт {r['horizon_h']}ч, "
          f"риск {r['risk_pct']:.0%}/сделку, стоп {r['stop_pct']:.0%} (риск = потеря при стопе, не размер позиции)\n")
    if r["trades"] == 0:
        print("Пока нет закрытых сделок: нужны сигналы с ценой входа И выхода спустя горизонт.")
        print("Копится автоматически — загляни через день-два.")
        return
    print(f"старт: ${r['start']:,.0f}  →  сейчас: ${r['balance']:,.0f}  "
          f"({r['pnl_pct']:+.2f}%, ${r['pnl']:+,.0f})")
    print(f"сделок: {r['trades']}  винрейт: {r['winrate']}%\n")
    print(f"из них закрыто по стопу: {r['stops']}\n")
    for t in r["closed"][-10:]:
        st = " СТОП" if t["stopped"] else ""
        print(f"  {t['symbol']:<8} {t['dir']:<5} {t['ret_pct']:+6.2f}%  ${t['pnl']:+8.2f}{st}")
    print("\nЭто виртуальный счёт по ФИКСИРОВАННОМУ правилу на реальных ценах — не сделки,")
    print("не Kelly, не автоторговля. Растёт демо-баланс — у правила есть смысл; падает —")
    print("правило не работает, и хорошо, что проверили на бумаге, а не на деньгах.")


def summary_text(r):
    lines = [f"<b>🎮 Демо-счёт (правило mean-reversion, {r['horizon_h']}ч)</b>"]
    if r["trades"] == 0:
        lines.append("Пока нет закрытых виртуальных сделок — копим историю, загляни позже.")
        return "\n".join(lines)
    emoji = "🟢" if r["pnl"] >= 0 else "🔴"
    lines.append(f"{emoji} <b>${r['balance']:,.0f}</b> из ${r['start']:,.0f} "
                 f"({r['pnl_pct']:+.2f}%)")
    lines.append(f"сделок: {r['trades']} · винрейт: {r['winrate']}% · по стопу: {r['stops']}")
    lines.append(f"<i>риск {r['risk_pct']:.0%}/сделку, стоп {r['stop_pct']:.0%} — риск = потеря при стопе, не размер позиции</i>")
    lines.append("\n<i>Виртуальные деньги, реальные цены, фиксированное правило. "
                 "Не сделки и не автоторговля — проверка стратегии на бумаге.</i>")
    return "\n".join(lines)


def selftest():
    import time as _t
    conn = history.connect(":memory:")
    now = int(_t.time())
    # oversold BTC @100, через 25ч цена 110 → long +10%
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "BTC", 100.0, "oversold"))
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "BTC", 110.0, None))
    conn.commit()
    r = simulate(conn, 24)
    assert r["trades"] == 1, r
    t = r["closed"][0]
    assert t["dir"] == "long" and abs(t["ret_pct"] - 10.0) < 1e-6, t
    # +10% raw, no stop; pnl in R = risk(1% of 10000=100) * (0.10/0.03) = 333.33
    assert abs(r["pnl"] - 333.33) < 0.1, r["pnl"]
    assert r["winrate"] == 100.0 and r["stops"] == 0

    # risk-management core: a move to the stop loses EXACTLY risk_pct of capital
    conn2 = history.connect(":memory:")
    conn2.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "X", 100.0, "oversold"))
    conn2.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "X", 97.0, None))  # -3% = stop
    conn2.commit()
    rs = simulate(conn2, 24)
    assert rs["stops"] == 1 and abs(rs["pnl"] - (-100.0)) < 1e-6, rs["pnl"]  # lost exactly 1% of 10000

    # overbought -> short, price up = loss
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "ETH", 100.0, "overbought"))
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "ETH", 120.0, None))
    conn.commit()
    r2 = simulate(conn, 24)
    eth = [t for t in r2["closed"] if t["symbol"] == "ETH"][0]
    assert eth["dir"] == "short" and eth["ret_pct"] < 0, eth  # short into a rise loses
    assert "Демо-счёт" in summary_text(r2)
    print("paper selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--horizon-hours", type=int, default=24)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest:
        selftest()
    else:
        conn = history.connect()
        r = simulate(conn, args.horizon_hours)
        if args.json:
            import json
            print(json.dumps(r, ensure_ascii=False))
        else:
            report(r)
        conn.close()

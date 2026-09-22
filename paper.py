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
RISK_PER_TRADE = 0.10      # 10% счёта на сделку, без плеча (демо, консервативно)


def _price_after(conn, symbol, after_ts, horizon_s):
    row = conn.execute(
        "SELECT ts, price FROM scout_runs WHERE symbol=? AND ts>=? AND price IS NOT NULL ORDER BY ts ASC LIMIT 1",
        (symbol, after_ts + horizon_s)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def simulate(conn, horizon_h=24):
    """Replay the mean-reversion rule over every logged oversold/overbought
    signal that already has a matching exit price `horizon_h` later. Returns
    the trade list and account stats."""
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
        ret = direction * (exit_price - entry) / entry   # доходность сделки
        pnl = balance * RISK_PER_TRADE * ret
        balance += pnl
        wins += ret > 0
        trades.append({"symbol": symbol, "signal": signal, "dir": "long" if direction > 0 else "short",
                       "entry": entry, "exit": exit_price, "ret_pct": ret * 100, "pnl": pnl})
    n = len(trades)
    return {
        "horizon_h": horizon_h, "trades": n,
        "start": START_BALANCE, "balance": round(balance, 2),
        "pnl": round(balance - START_BALANCE, 2),
        "pnl_pct": round((balance / START_BALANCE - 1) * 100, 2),
        "winrate": round(wins / n * 100, 1) if n else None,
        "closed": trades,
    }


def report(r):
    print(f"<Демо-счёт> правило: mean-reversion по RSI, горизонт {r['horizon_h']}ч, "
          f"{RISK_PER_TRADE:.0%} счёта/сделку, без плеча\n")
    if r["trades"] == 0:
        print("Пока нет закрытых сделок: нужны сигналы с ценой входа И выхода спустя горизонт.")
        print("Копится автоматически — загляни через день-два.")
        return
    print(f"старт: ${r['start']:,.0f}  →  сейчас: ${r['balance']:,.0f}  "
          f"({r['pnl_pct']:+.2f}%, ${r['pnl']:+,.0f})")
    print(f"сделок: {r['trades']}  винрейт: {r['winrate']}%\n")
    for t in r["closed"][-10:]:
        print(f"  {t['symbol']:<8} {t['dir']:<5} {t['ret_pct']:+6.2f}%  ${t['pnl']:+8.2f}")
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
    lines.append(f"сделок: {r['trades']} · винрейт: {r['winrate']}%")
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
    # +10% on 10% of 10000 = +100
    assert abs(r["pnl"] - 100.0) < 1e-6, r["pnl"]
    assert r["winrate"] == 100.0

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

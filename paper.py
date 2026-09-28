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

FEE_PCT = 0.00055          # Bybit taker за сторону (обычный аккаунт, без VIP)
SLIP_PCT = 0.0005          # проскальзывание за сторону — оценка, подкрути по факту
COST_PCT = 2 * (FEE_PCT + SLIP_PCT)  # вход+выход = 0.21% от НОМИНАЛА позиции
# Номинал = риск / стоп, поэтому тугой стоп = большая позиция = больше комиссий:
# при стопе 2% издержки съедают ~0.1R на сделку, при 5% — ~0.04R.
# ponytail: фандинг за время удержания не учтён (может быть и плюсом, и минусом).


def _price_after(conn, symbol, after_ts, horizon_s):
    row = conn.execute(
        "SELECT ts, price FROM scout_runs WHERE symbol=? AND ts>=? AND price IS NOT NULL ORDER BY ts ASC LIMIT 1",
        (symbol, after_ts + horizon_s)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _fetch_lowhigh(perp, start_ms, end_ms, interval="60"):
    """Real intrabar high/low path from Bybit, oldest-first [(ts_ms, high, low)].
    1h candles: an hourly high/low IS the intrabar extreme for that hour — enough
    to know if a stop was ever hit. Bybit caps a call at 1000 candles (~41 days)
    and returns the NEWEST ones, so page backwards until start is covered.
    Returns None if the coin has no Bybit perp or the call is blocked; the caller
    then falls back to the honest-but-coarse horizon-only check.
    # ponytail: 1h resolution; drop to 15m only if a sub-hour round-trip past
    # the stop turns out to matter."""
    import scalp
    out, end = [], end_ms
    try:
        while end > start_ms:
            rows = scalp.get_json(
                f"{scalp.BYBIT}/market/kline?category=linear&symbol={perp}"
                f"&interval={interval}&start={start_ms}&end={end}&limit=1000")["list"]
            out += [(int(c[0]), float(c[2]), float(c[3])) for c in rows]
            if len(rows) < 1000:
                break
            end = int(rows[-1][0]) - 1  # rows newest-first → last is the oldest
    except Exception:
        return None
    return sorted(set(out)) or None


def build_paths(conn, horizon_h, interval="60", sleep_s=0.15):
    """One Bybit kline fetch per distinct flagged coin, covering its whole
    signal span — so simulate() can check stops intrabar without a call per
    trade. Spaced out (sleep_s) so a burst doesn't trip Bybit's CloudFront WAF."""
    import time as _t
    syms = conn.execute(
        "SELECT symbol, MIN(ts), MAX(ts) FROM scout_runs "
        "WHERE signal IN ('oversold','overbought') AND price IS NOT NULL GROUP BY symbol").fetchall()
    paths = {}
    for sym, lo, hi in syms:
        # −1ч: свеча, содержащая первый вход, начинается раньше самого входа
        paths[sym] = _fetch_lowhigh(sym + "USDT", (lo - 3600) * 1000, (hi + horizon_h * 3600) * 1000, interval)
        _t.sleep(sleep_s)
    return paths


def _stopped_intrabar(path, entry_ts, exit_ts, entry, direction, stop_pct):
    """Was the stop breached between entry and exit? long: a low <= entry*(1-stop);
    short: a high >= entry*(1+stop). None means 'no path' → caller falls back."""
    if not path:
        return None
    lo_ms, hi_ms = entry_ts * 1000, exit_ts * 1000
    # candle that CONTAINS the entry counts too (its start is up to 1h before entry)
    window = [(h, l) for ts, h, l in path if lo_ms - 3_600_000 < ts <= hi_ms]
    if not window or path[0][0] > lo_ms:
        return None  # путь не покрывает сделку — не притворяемся, что стопа не было
    if direction > 0:
        floor = entry * (1 - stop_pct)
        return any(l <= floor for _, l in window)
    ceil = entry * (1 + stop_pct)
    return any(h >= ceil for h, _ in window)


def simulate(conn, horizon_h=24, stop_pct=STOP_PCT, risk_pct=RISK_PCT, paths=None, cost_pct=COST_PCT):
    """Replay the mean-reversion rule with PROPER risk management: each trade
    risks a fixed risk_pct of capital, capped by a stop_pct stop-loss. Position
    size follows from that, not the other way round. A trade closes at the stop
    (loss = risk_pct) if the stop was hit, otherwise at the horizon price.

    Stop detection has two modes. Default (paths=None): checked only at the
    horizon, not intrabar — coarse and OPTIMISTIC (a dip that hit the stop then
    recovered by the horizon is missed, so tight stops look better than real).
    With `paths` (from build_paths): checked against real 1h high/low between
    entry and exit — the honest version. `intrabar` in the result says which ran.

    cost_pct: round-trip fees+slippage as a fraction of the position's notional
    (notional = risk$ / stop), charged on every trade, win or lose."""
    rows = conn.execute(
        "SELECT symbol, ts, price, signal FROM scout_runs "
        "WHERE signal IN ('oversold','overbought') AND price IS NOT NULL ORDER BY ts ASC").fetchall()
    balance = START_BALANCE
    trades, wins, approx, fees = [], 0, 0, 0.0
    for symbol, ts, entry, signal in rows:
        exit_ts, exit_price = _price_after(conn, symbol, ts, horizon_h * 3600)
        if not exit_price:
            continue  # позиция ещё «открыта» — нет цены выхода в истории
        direction = 1 if signal == "oversold" else -1  # long / short
        raw_ret = direction * (exit_price - entry) / entry
        # стоп: intrabar по реальным свечам, если есть путь; иначе — на горизонте
        hit = _stopped_intrabar(paths.get(symbol) if paths else None,
                                ts, exit_ts, entry, direction, stop_pct)
        if hit is None:              # нет пути (нет перпа/блок) — грубый фолбэк
            hit = raw_ret <= -stop_pct
            approx += 1
        # риск-$ фиксирован; размер такой, что движение на stop_pct = потеря risk_pct
        risk_dollars = balance * risk_pct
        if hit:
            pnl = -risk_dollars                       # стоп: теряем ровно заложенный риск
            realized_ret = -stop_pct
        else:
            pnl = risk_dollars * (raw_ret / stop_pct)  # прибыль/убыток в единицах риска (R)
            realized_ret = raw_ret
        cost = risk_dollars / stop_pct * cost_pct      # комиссии+слиппедж от номинала
        pnl -= cost
        fees += cost
        balance += pnl
        wins += pnl > 0                                # победа — только если в плюсе ПОСЛЕ издержек
        trades.append({"symbol": symbol, "signal": signal, "dir": "long" if direction > 0 else "short",
                       "entry": entry, "exit": exit_price, "ret_pct": (realized_ret - cost_pct) * 100,
                       "pnl": pnl, "stopped": hit})
    n = len(trades)
    return {
        "horizon_h": horizon_h, "trades": n,
        "start": START_BALANCE, "balance": round(balance, 2),
        "pnl": round(balance - START_BALANCE, 2),
        "pnl_pct": round((balance / START_BALANCE - 1) * 100, 2),
        "winrate": round(wins / n * 100, 1) if n else None,
        "stops": sum(1 for t in trades if t["stopped"]),
        "risk_pct": risk_pct, "stop_pct": stop_pct, "cost_pct": cost_pct,
        "fees": round(fees, 2),
        "intrabar": bool(paths), "approx_trades": approx,
        "closed": trades,
    }


def compare_stops(conn, horizon_h=24, stops=(0.02, 0.03, 0.05), risk_pct=RISK_PCT, paths=None, cost_pct=COST_PCT):
    """Same trades, different stop-loss %. Shows how much the stop alone moves
    the outcome — the video's whole point that risk management, not the signal,
    drives survival. Pass `paths` (build_paths) for honest intrabar stops."""
    return [{"stop_pct": s, **{k: simulate(conn, horizon_h, s, risk_pct, paths, cost_pct)[k]
                               for k in ("pnl_pct", "winrate", "stops", "trades", "fees", "approx_trades")}}
            for s in stops]


def compare_text(rows, horizon_h, intrabar, cost_pct=COST_PCT):
    """Telegram table for the bot's ⚖️ Стопы screen; best stop is starred."""
    if not rows or not rows[0]["trades"]:
        return f"⚖️ <b>Стопы, {horizon_h}ч</b>\nПока нет закрытых сделок на этом горизонте — копим историю."
    best = max(rows, key=lambda x: x["pnl_pct"])
    lines = [f"⚖️ <b>Стопы · горизонт {horizon_h}ч · {rows[0]['trades']} сделок</b>",
             "<code>стоп     P&amp;L  винрейт  стопов</code>"]
    for x in rows:
        star = " ⭐" if x is best else ""
        lines.append(f"<code>{x['stop_pct']*100:>3.0f}% {x['pnl_pct']:>+8.1f}% {(x['winrate'] or 0):>6.0f}% {x['stops']:>6}</code>{star}")
    mode = "стопы по реальным 1ч-свечам Bybit" if intrabar else "⚠️ стоп только на горизонте (оптимистично)"
    lines.append(f"\n<i>{mode} · комиссии+слиппедж {cost_pct*100:.2f}% за круг · риск 1%/сделку</i>")
    lines.append("<i>Винрейт считается ПОСЛЕ издержек. Это бумага, не совет.</i>")
    return "\n".join(lines)


def report(r):
    print(f"<Демо-счёт> mean-reversion по RSI, горизонт {r['horizon_h']}ч, "
          f"риск {r['risk_pct']:.0%}/сделку, стоп {r['stop_pct']:.0%} (риск = потеря при стопе, не размер позиции)\n")
    if r["trades"] == 0:
        print("Пока нет закрытых сделок: нужны сигналы с ценой входа И выхода спустя горизонт.")
        print("Копится автоматически — загляни через день-два.")
        return
    print(f"старт: ${r['start']:,.0f}  →  сейчас: ${r['balance']:,.0f}  "
          f"({r['pnl_pct']:+.2f}%, ${r['pnl']:+,.0f})")
    print(f"сделок: {r['trades']}  винрейт (после издержек): {r['winrate']}%")
    print(f"из них закрыто по стопу: {r['stops']}  ·  комиссии+слиппедж: ${r['fees']:,.2f}\n")
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
    lines.append(f"комиссии+слиппедж съели: <b>${r['fees']:,.0f}</b>")
    mode = "стопы по реальным 1ч-свечам" if r.get("intrabar") else "⚠️ стоп только на горизонте"
    lines.append(f"<i>риск {r['risk_pct']:.0%}/сделку, стоп {r['stop_pct']:.0%}, {mode}, "
                 f"издержки {r['cost_pct']*100:.2f}% за круг</i>")
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
    r = simulate(conn, 24, cost_pct=0)
    assert r["trades"] == 1, r
    t = r["closed"][0]
    assert t["dir"] == "long" and abs(t["ret_pct"] - 10.0) < 1e-6, t
    # +10% raw, no stop; pnl in R = risk(1% of 10000=100) * (0.10/0.03) = 333.33
    assert abs(r["pnl"] - 333.33) < 0.1, r["pnl"]
    assert r["winrate"] == 100.0 and r["stops"] == 0
    # costs: notional = 100/0.03 = 3333.33; 0.21% of it = 7.00 → pnl 326.33
    rc = simulate(conn, 24)
    assert abs(rc["fees"] - 7.0) < 0.01 and abs(rc["pnl"] - 326.33) < 0.01, (rc["fees"], rc["pnl"])
    # tighter stop = bigger notional = bigger fees on the same trade
    assert simulate(conn, 24, stop_pct=0.02)["fees"] > simulate(conn, 24, stop_pct=0.05)["fees"]
    # a tiny green move that doesn't cover costs is a LOSS, not a win
    connw = history.connect(":memory:")
    connw.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "W", 100.0, "oversold"))
    connw.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "W", 100.1, None))  # +0.1%
    connw.commit()
    assert simulate(connw, 24)["winrate"] == 0.0, "+0.1% < 0.21% costs must not count as a win"

    # risk-management core: a move to the stop loses EXACTLY risk_pct of capital
    conn2 = history.connect(":memory:")
    conn2.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "X", 100.0, "oversold"))
    conn2.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "X", 97.0, None))  # -3% = stop
    conn2.commit()
    rs = simulate(conn2, 24, cost_pct=0)
    assert rs["stops"] == 1 and abs(rs["pnl"] - (-100.0)) < 1e-6, rs["pnl"]  # lost exactly 1% of 10000

    # overbought -> short, price up = loss
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "ETH", 100.0, "overbought"))
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "ETH", 120.0, None))
    conn.commit()
    r2 = simulate(conn, 24)
    eth = [t for t in r2["closed"] if t["symbol"] == "ETH"][0]
    assert eth["dir"] == "short" and eth["ret_pct"] < 0, eth  # short into a rise loses
    assert "Демо-счёт" in summary_text(r2)

    # intrabar stop: a long that's GREEN at the horizon but dipped past the stop
    # in between must be counted stopped — the whole reason horizon-only lies.
    conn3 = history.connect(":memory:")
    conn3.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "Z", 100.0, "oversold"))
    conn3.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "Z", 108.0, None))  # +8% by horizon
    conn3.commit()
    # path dips to 96 (-4%) mid-window, then recovers — a 3% stop should trigger
    path = {"Z": [(int((now - 1800) * 1000), 100.5, 99.5),       # candle containing the entry
                  (int((now + 5*3600) * 1000), 101.0, 96.0),
                  (int((now + 10*3600) * 1000), 109.0, 100.0)]}
    horizon_only = simulate(conn3, 24, cost_pct=0)
    intrabar = simulate(conn3, 24, paths=path, cost_pct=0)
    assert horizon_only["stops"] == 0, "horizon-only misses the mid-window dip"
    assert intrabar["stops"] == 1 and intrabar["intrabar"], "intrabar must catch the -4% dip past a 3% stop"
    assert intrabar["closed"][0]["ret_pct"] == -3.0, intrabar["closed"][0]
    # no path for a symbol → falls back, counted in approx_trades
    assert simulate(conn3, 24, paths={})["approx_trades"] == 1
    # path that starts AFTER the entry (truncated history) → fallback, not a fake "no stop"
    late = {"Z": [(int((now + 5*3600) * 1000), 109.0, 100.0)]}
    assert simulate(conn3, 24, paths=late)["approx_trades"] == 1
    assert "Стопы" in compare_text(compare_stops(conn3, 24, paths=path), 24, True)
    print("paper selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--horizon-hours", type=int, default=24)
    p.add_argument("--stop", type=float, default=STOP_PCT * 100, help="стоп-лосс в %% (напр. 2, 3, 5)")
    p.add_argument("--risk", type=float, default=RISK_PCT * 100, help="риск на сделку в %% капитала")
    p.add_argument("--compare", action="store_true", help="сравнить стопы 2/3/5%% на тех же сделках")
    p.add_argument("--intrabar", action="store_true",
                    help="проверять стоп по реальным 1ч-свечам Bybit (честно, но медленнее)")
    p.add_argument("--cost", type=float, default=COST_PCT * 100,
                    help="комиссии+слиппедж за круг в %% от номинала (0 — без издержек)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest:
        selftest()
    else:
        conn = history.connect()
        paths = None
        if args.intrabar:
            print("тяну реальные 1ч-свечи с Bybit для intrabar-стопов…")
            paths = build_paths(conn, args.horizon_hours)
            have = sum(1 for v in paths.values() if v)
            print(f"путь есть у {have}/{len(paths)} монет (у остальных — грубый фолбэк на горизонте)")
        if args.compare:
            rows = compare_stops(conn, args.horizon_hours, risk_pct=args.risk / 100, paths=paths,
                                 cost_pct=args.cost / 100)
            mode = "intrabar по 1ч-свечам" if paths else "стоп на горизонте (оптимистично)"
            print(f"Сравнение стопов (риск {args.risk:.0f}%/сделку, горизонт {args.horizon_hours}ч, {mode}, "
                  f"издержки {args.cost:.2f}%):\n")
            print(f"{'стоп':>6}{'P&L':>10}{'винрейт':>10}{'по стопу':>10}{'сделок':>9}")
            for x in rows:
                print(f"{x['stop_pct']*100:>5.0f}%{x['pnl_pct']:>9.2f}%{(x['winrate'] or 0):>9.1f}%{x['stops']:>10}{x['trades']:>9}")
            if paths:
                print(f"\nстоп по грубому фолбэку (нет свечей): {rows[0]['approx_trades']} из {rows[0]['trades']} сделок")
        else:
            r = simulate(conn, args.horizon_hours, args.stop / 100, args.risk / 100, paths, args.cost / 100)
            if args.json:
                import json
                print(json.dumps(r, ensure_ascii=False))
            else:
                report(r)
        conn.close()

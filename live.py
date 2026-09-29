#!/usr/bin/env python3
"""⚡💥 Real-time alerts → Telegram, for a trader who acts on them by hand.

  ⚡ Funding spikes: every minute ONE tickers call covers every Bybit USDT perp.
     A coin whose rate PER ITS OWN PERIOD reaches ±fund_alert% (config, default
     1%) is alerted once per settlement period — again only if it doubles. The
     alert carries what decides tradability: period, minutes to payout, who pays,
     1h price move, 24h turnover, open interest.
  💥 Liquidation cascades: Bybit's public allLiquidation WebSocket for every perp
     with some liquidity. When one side gets liquidated for ≥ max($300k, 0.1% of
     the coin's daily turnover) within 60 s, that is a cascade — the squeeze you
     trade. Every minute's liquidations also go to history.db (liq_minutes): no
     exchange serves liquidation history, so this is our own archive for backtests.

    python3 live.py              # run (systemd: jev-live.service)
    python3 live.py --selftest

Needs `websocket-client` (apt python3-websocket / pip websocket-client) for the
liquidation stream; funding alerts work without it.
"""
import collections, html, json, sys, threading, time

import botconfig
import history
import scalp
import telegram_notify as tn

WS_URL = "wss://stream.bybit.com/v5/public/linear"
LIQ_WINDOW_S = 60
LIQ_MIN_USD = 300_000
LIQ_SHARE = 0.001          # доля дневного оборота за минуту
LIQ_COOLDOWN_S = 600
LIQ_MIN_TURNOVER = 1e6     # монеты тоньше не слушаем
FUND_EVERY_S = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS liq_minutes (
    ts INTEGER NOT NULL, symbol TEXT NOT NULL, long_usd REAL, short_usd REAL, n INTEGER,
    PRIMARY KEY (ts, symbol));
CREATE TABLE IF NOT EXISTS funding_alerts (
    ts INTEGER NOT NULL, symbol TEXT NOT NULL, rate REAL, interval_h REAL, next_min INTEGER,
    turnover REAL, oi REAL, move_1h REAL);
"""


def connect(path=None):
    conn = history.connect(path)
    conn.executescript(SCHEMA)
    return conn


# ---------- ⚡ фандинг ----------

def funding_hits(tickers, instruments, threshold_pct, now_ms):
    """Tickers whose funding rate per ITS period is ≥ threshold (%). Returns dicts."""
    out = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT") or not t.get("fundingRate"):
            continue
        rate = float(t["fundingRate"])
        if abs(rate) * 100 < threshold_pct:
            continue
        last, p1h = float(t.get("lastPrice") or 0), float(t.get("prevPrice1h") or 0)
        nxt = int(t.get("nextFundingTime") or 0)
        out.append({"symbol": s, "rate": rate,
                    "interval_h": instruments.get(s, {}).get("funding_interval_min", 480) / 60,
                    "next_ms": nxt, "next_min": max(0, round((nxt - now_ms) / 60000)) if nxt else None,
                    "turnover": float(t.get("turnover24h") or 0), "oi": float(t.get("openInterestValue") or 0),
                    "move_1h": (last / p1h - 1) * 100 if p1h else None})
    return sorted(out, key=lambda x: -abs(x["rate"]))


def funding_line(h):
    who = "🔴 лонги платят шортам" if h["rate"] > 0 else "🟢 шорты платят лонгам"
    m = h["next_min"]
    left = f"{m // 60}ч{m % 60:02d}м" if m is not None and m >= 60 else f"{m} мин"
    move = f" · цена за 1ч {h['move_1h']:+.1f}%" if h["move_1h"] is not None else ""
    return (f"⚡ <b>{html.escape(h['symbol'].replace('USDT', ''))}</b> фандинг <b>{h['rate'] * 100:+.2f}%</b> "
            f"за {h['interval_h']:g}ч-период · {who} · до выплаты {left}{move}"
            f" · оборот ${h['turnover'] / 1e6:,.1f}M · OI ${h['oi'] / 1e6:,.1f}M")


class FundingWatch:
    """Alert once per (symbol, settlement); re-alert only if the rate doubles."""

    def __init__(self):
        self.sent = {}

    def check(self, hits):
        lines = []
        for h in hits:
            key = (h["symbol"], h["next_ms"])
            prev = self.sent.get(key)
            if prev is None or abs(h["rate"]) >= 2 * abs(prev):
                self.sent[key] = h["rate"]
                lines.append(funding_line(h))
        return lines


# ---------- 💥 ликвидации ----------

class LiqWatch:
    """Rolling 60 s window per (symbol, side). Bybit allLiquidation: S="Buy" means
    a LONG was liquidated, S="Sell" a SHORT."""

    def __init__(self, turnover):
        self.turnover = turnover          # symbol -> 24h turnover $
        self.win = collections.defaultdict(collections.deque)
        self.last_alert = {}              # (symbol, side) -> (ts, usd)
        self.minute = collections.defaultdict(lambda: [0.0, 0.0, 0])
        self.lock = threading.Lock()

    def threshold(self, sym):
        return max(LIQ_MIN_USD, LIQ_SHARE * self.turnover.get(sym, 0))

    def add(self, sym, side_raw, size, price, ts_ms):
        """Returns an alert dict when a cascade fires, else None."""
        side = "long" if side_raw == "Buy" else "short"
        usd = size * price
        ts = ts_ms / 1000
        with self.lock:
            m = self.minute[(int(ts // 60) * 60, sym)]
            m[0 if side == "long" else 1] += usd
            m[2] += 1
            w = self.win[(sym, side)]
            w.append((ts, usd))
            while w and w[0][0] < ts - LIQ_WINDOW_S:
                w.popleft()
            total = sum(u for _, u in w)
            if total < self.threshold(sym):
                return None
            prev = self.last_alert.get((sym, side))
            if prev and ts - prev[0] < LIQ_COOLDOWN_S and total < 2 * prev[1]:
                return None
            self.last_alert[(sym, side)] = (ts, total)
            return {"symbol": sym, "side": side, "usd": total, "n": len(w), "price": price,
                    "share": total / self.turnover[sym] * 100 if self.turnover.get(sym) else None}

    def flush(self, conn, before_ts):
        """Write finished minutes to history.db."""
        with self.lock:
            done = [(k, v) for k, v in self.minute.items() if k[0] < before_ts]
            for k, _ in done:
                del self.minute[k]
        conn.executemany("INSERT OR REPLACE INTO liq_minutes VALUES (?,?,?,?,?)",
                         [(ts, s, v[0], v[1], v[2]) for (ts, s), v in done])
        conn.commit()


def liq_line(a, move_1h=None):
    who = "ЛОНГИ 🔻" if a["side"] == "long" else "ШОРТЫ 🔺"
    share = f" ({a['share']:.2f}% дневного оборота)" if a.get("share") else ""
    move = f" · цена за 1ч {move_1h:+.1f}%" if move_1h is not None else ""
    return (f"💥 <b>{html.escape(a['symbol'].replace('USDT', ''))}</b> каскад: ликвидируют {who} "
            f"<b>${a['usd'] / 1e6:,.2f}M</b> за 60 сек, {a['n']} шт{share} · цена {a['price']:,.6g}{move}")


def liq_stream(watch, symbols, out_queue, stop):
    """WebSocket loop with reconnect; pushes alert dicts into out_queue."""
    import websocket

    def on_open(ws):
        for i in range(0, len(symbols), 10):
            ws.send(json.dumps({"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols[i:i + 10]]}))

    def on_message(ws, msg):
        d = json.loads(msg)
        for e in d.get("data") or []:
            a = watch.add(e["s"], e["S"], float(e["v"]), float(e["p"]), int(e["T"]))
            if a:
                out_queue.append(a)

    def ping(ws):  # Bybit рвёт соединение без пинга раз в ~20 с
        while True:
            time.sleep(20)
            try:
                ws.send('{"op":"ping"}')
            except Exception:
                return

    while not stop.is_set():
        ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message)
        threading.Thread(target=ping, args=(ws,), daemon=True).start()
        try:
            ws.run_forever()
        except Exception as e:
            print(f"ws error: {e!r}", file=sys.stderr, flush=True)
        time.sleep(5)


# ---------- главный цикл ----------

def main():
    conn = connect()
    inst = scalp.fetch_instruments() or {}
    tickers = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
    turnover = {t["symbol"]: float(t.get("turnover24h") or 0) for t in tickers}
    symbols = [s for s, v in turnover.items() if s.endswith("USDT") and v >= LIQ_MIN_TURNOVER]
    liq, alerts, stop = LiqWatch(turnover), collections.deque(), threading.Event()
    try:
        import websocket  # noqa: F401
        threading.Thread(target=liq_stream, args=(liq, symbols, alerts, stop), daemon=True).start()
        print(f"live: ликвидации по {len(symbols)} монетам, фандинг по всем", flush=True)
    except ImportError:
        print("live: нет websocket-client — только фандинг-алерты", file=sys.stderr, flush=True)
    fw, last_fund, moves = FundingWatch(), 0, {}
    while True:
        try:
            cfg = botconfig.load()
            now = time.time()
            if now - last_fund >= FUND_EVERY_S:
                last_fund = now
                tickers = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
                for t in tickers:
                    turnover[t["symbol"]] = float(t.get("turnover24h") or 0)
                    last, p1h = float(t.get("lastPrice") or 0), float(t.get("prevPrice1h") or 0)
                    moves[t["symbol"]] = (last / p1h - 1) * 100 if p1h else None
                hits = funding_hits(tickers, inst, float(cfg.get("fund_alert", 1.0)), now * 1000)
                for h in hits:
                    conn.execute("INSERT INTO funding_alerts VALUES (?,?,?,?,?,?,?,?)",
                                 (int(now), h["symbol"], h["rate"], h["interval_h"], h["next_min"],
                                  h["turnover"], h["oi"], h["move_1h"]))
                conn.commit()
                lines = fw.check(hits) if cfg.get("fund_alerts", True) else []
                if lines:
                    tn._send("\n".join(lines))
                liq.flush(conn, int(now // 60) * 60)
            out = []
            while alerts:
                a = alerts.popleft()
                out.append(liq_line(a, moves.get(a["symbol"])))
            if out and cfg.get("liq_alerts", True):
                tn._send("\n".join(out))
        except Exception as e:
            print(f"live loop error: {e!r}", file=sys.stderr, flush=True)
        time.sleep(2)


def summary_text(conn, hours=1, top=8):
    """Bot screen: biggest liquidations of the last hour(s)."""
    since = int(time.time()) - hours * 3600
    rows = conn.execute("SELECT symbol, SUM(long_usd), SUM(short_usd), SUM(n) FROM liq_minutes WHERE ts >= ? "
                        "GROUP BY symbol ORDER BY SUM(long_usd)+SUM(short_usd) DESC LIMIT ?", (since, top)).fetchall()
    tot = conn.execute("SELECT SUM(long_usd), SUM(short_usd) FROM liq_minutes WHERE ts >= ?", (since,)).fetchone()
    lines = [f"💥 <b>Ликвидации за {hours} ч</b> (Bybit, все монеты)"]
    if not rows:
        lines.append("Пока пусто — поток пишется с момента запуска.")
        return "\n".join(lines)
    lines.append(f"всего: лонги ${(tot[0] or 0) / 1e6:,.1f}M 🔻 · шорты ${(tot[1] or 0) / 1e6:,.1f}M 🔺\n")
    for s, l, sh, n in rows:
        lines.append(f"<b>{html.escape(s.replace('USDT', ''))}</b> лонги ${l / 1e6:,.2f}M · шорты ${sh / 1e6:,.2f}M ({n} шт)")
    lines.append("\n<i>Каскад (одна сторона ≥ max($300k, 0.1% дневного оборота) за минуту) приходит алертом сразу.</i>")
    return "\n".join(lines)


def funding_text(threshold_pct, top=12):
    """Bot screen: live top funding PER PERIOD across all Bybit perps."""
    tickers = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
    hits = funding_hits(tickers, scalp.fetch_instruments() or {}, 0, time.time() * 1000)[:top]
    lines = [f"⚡ <b>Фандинг сейчас</b> — самые большие ставки за период, весь Bybit",
             f"алерт приходит сам от <b>±{threshold_pct:g}%</b> (меняется в ⚙️ Настройках)\n"]
    lines += [funding_line(h) for h in hits]
    return "\n".join(lines)


def selftest():
    t = [{"symbol": "AUSDT", "fundingRate": "0.012", "lastPrice": "110", "prevPrice1h": "100",
          "nextFundingTime": str(1_000_000 + 37 * 60000), "turnover24h": "400000", "openInterestValue": "1200000"},
         {"symbol": "BUSDT", "fundingRate": "-0.004", "lastPrice": "1", "prevPrice1h": "1", "nextFundingTime": "0",
          "turnover24h": "9e6", "openInterestValue": "1"},
         {"symbol": "CPERP", "fundingRate": "0.5"}]
    hits = funding_hits(t, {"AUSDT": {"funding_interval_min": 240}}, 1.0, 1_000_000)
    assert [h["symbol"] for h in hits] == ["AUSDT"] and hits[0]["next_min"] == 37 and hits[0]["interval_h"] == 4
    line = funding_line(hits[0])
    assert "+1.20%" in line and "4ч-период" in line and "лонги платят" in line and "37 мин" in line and "+10.0%" in line
    assert "годовых" not in line
    fw = FundingWatch()
    assert len(fw.check(hits)) == 1 and fw.check(hits) == []            # once per period
    hits[0]["rate"] = 0.025
    assert len(fw.check(hits)) == 1                                      # doubled → again

    w = LiqWatch({"SOLUSDT": 1e9, "XUSDT": 1e6})
    assert w.threshold("SOLUSDT") == 1e6 and w.threshold("XUSDT") == LIQ_MIN_USD
    T = 1_700_000_000_000
    assert w.add("XUSDT", "Buy", 1000, 100, T) is None                   # $100k < $300k
    a = w.add("XUSDT", "Buy", 2500, 100, T + 10_000)                      # $350k within 60 s → cascade
    assert a and a["side"] == "long" and a["usd"] == 350_000 and a["n"] == 2
    assert w.add("XUSDT", "Buy", 100, 100, T + 20_000) is None           # cooldown, not doubled
    assert w.add("XUSDT", "Sell", 4000, 100, T + 21_000)["side"] == "short"
    assert w.add("XUSDT", "Buy", 1000, 100, T + 200_000) is None         # window expired
    assert "ЛОНГИ" in liq_line(a, -3.2) and "-3.2%" in liq_line(a, -3.2)
    conn = connect(":memory:")
    w.flush(conn, 2_000_000_000)
    long_usd, short_usd, n = conn.execute("SELECT SUM(long_usd), SUM(short_usd), SUM(n) FROM liq_minutes").fetchone()
    assert long_usd == 100_000 + 250_000 + 10_000 + 100_000 and short_usd == 400_000 and n == 5
    assert "Ликвидации" in summary_text(conn, hours=10**6)
    print("live selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()

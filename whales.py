#!/usr/bin/env python3
"""🐋 Whale radar — live positions of Hyperliquid's top traders.

Hyperliquid is an on-chain perp exchange: every wallet's open positions are
public through its info API. We take the traders who are in profit both over
the last month and all-time (the leaderboard, minus exchange vaults), poll
their positions every couple of minutes, and turn changes into events:
opened / closed / flipped / added / cut. Big ones go to Telegram; all of them
go to history.db so we can later measure whether following a given whale would
have made money (paper-follow) — a long here may just hedge a short elsewhere.

    python3 whales.py --once          # one poll (first poll per wallet = baseline, no alerts)
    python3 whales.py --loop 120      # run forever, poll every 120s (systemd)
    python3 whales.py --consensus     # where the whales are positioned now
    python3 whales.py --selftest
"""
import argparse, html, json, sys, time, urllib.request

import history

INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
N_WHALES = 50
MIN_TRACK = 5e5        # $ — позиции меньше считаем шумом
MIN_ALERT = 5e6        # $ — от этой суммы (или изменения) шлём алерт
REFRESH_S = 6 * 3600   # как часто перечитывать лидерборд

SCHEMA = """
CREATE TABLE IF NOT EXISTS whale_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, addr TEXT NOT NULL,
    coin TEXT NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL,
    value REAL, price REAL, lev REAL, month_pnl REAL);
CREATE INDEX IF NOT EXISTS idx_whale_ev ON whale_events(addr, coin, ts);
CREATE TABLE IF NOT EXISTS whale_state (
    addr TEXT NOT NULL, coin TEXT NOT NULL, szi REAL, value REAL, entry REAL, lev REAL,
    PRIMARY KEY (addr, coin));
CREATE TABLE IF NOT EXISTS whale_seen (addr TEXT PRIMARY KEY, since INTEGER);
"""

KIND_RU = {"open": "открыл", "close": "закрыл", "flip": "перевернулся в", "add": "добавил в", "cut": "сократил"}


def connect(path=None):
    conn = history.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _json(req):
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def info(body):
    return _json(urllib.request.Request(INFO, data=json.dumps(body).encode(),
                                        headers={"Content-Type": "application/json"}))


def perf(row, window):
    return next((float(p[1]["pnl"]) for p in row["windowPerformances"] if p[0] == window), 0.0)


def pick_whales(rows, n=N_WHALES):
    """Profitable over the month AND all-time; $1M–$300M accounts (above that
    are exchange vaults like HLP, below is noise)."""
    ok = [r for r in rows if 1e6 <= float(r["accountValue"]) <= 3e8
          and perf(r, "month") > 0 and perf(r, "allTime") > 0]
    return sorted(ok, key=lambda r: perf(r, "month"), reverse=True)[:n]


def positions(addr):
    st = info({"type": "clearinghouseState", "user": addr})
    out = {}
    for p in st["assetPositions"]:
        p = p["position"]
        v = float(p["positionValue"])
        if v >= MIN_TRACK:
            out[p["coin"]] = {"szi": float(p["szi"]), "value": v,
                              "entry": float(p.get("entryPx") or 0), "lev": float(p["leverage"]["value"])}
    return out


def diff(prev, cur):
    """Events between two {coin: position} snapshots of one wallet.
    add/cut carry the CHANGE in $, open/flip/close the full position."""
    ev = []
    for c, p in cur.items():
        side = "long" if p["szi"] > 0 else "short"
        q = prev.get(c)
        if q is None:
            ev.append(("open", c, side, p["value"]))
        elif (q["szi"] > 0) != (p["szi"] > 0):
            ev.append(("flip", c, side, p["value"]))
        elif abs(p["szi"]) >= abs(q["szi"]) * 1.5:
            ev.append(("add", c, side, p["value"] - q["value"]))
        elif abs(p["szi"]) <= abs(q["szi"]) * 0.5:
            ev.append(("cut", c, side, q["value"] - p["value"]))
    for c, q in prev.items():
        if c not in cur:
            ev.append(("close", c, "long" if q["szi"] > 0 else "short", q["value"]))
    return ev


def short_addr(a):
    return f"{a[:6]}…{a[-4:]}"


def alert_line(addr, month_pnl, kind, coin, side, value, lev, price):
    arrow = "🟢 ЛОНГ" if side == "long" else "🔴 ШОРТ"
    px = f" @ {price:,.6g}" if price else ""
    levs = f" ×{lev:g}" if lev else ""
    return (f'🐋 <a href="https://hypurrscan.io/address/{addr}">{short_addr(addr)}</a> '
            f"(+${month_pnl / 1e6:.1f}M за мес) {KIND_RU[kind]} {arrow} <b>{html.escape(coin)}</b> "
            f"${value / 1e6:.1f}M{levs}{px}")


def cycle(conn, whales, now=None, fetch=positions, mids=None):
    """One poll over all whales. First sight of a wallet is a silent baseline.
    Returns alert lines (value/change ≥ MIN_ALERT)."""
    now = now or int(time.time())
    if mids is None:
        mids = {k: float(v) for k, v in info({"type": "allMids"}).items()}
    alerts = []
    for r in whales:
        a = r["ethAddress"]
        try:
            cur = fetch(a)
        except Exception:
            continue
        seen = conn.execute("SELECT 1 FROM whale_seen WHERE addr=?", (a,)).fetchone()
        prev = {c: {"szi": s, "value": v, "entry": e, "lev": l} for c, s, v, e, l in
                conn.execute("SELECT coin, szi, value, entry, lev FROM whale_state WHERE addr=?", (a,))}
        if seen:
            for kind, coin, side, value in diff(prev, cur):
                lev = (cur.get(coin) or prev.get(coin) or {}).get("lev")
                price = mids.get(coin)
                conn.execute("INSERT INTO whale_events (ts, addr, coin, kind, side, value, price, lev, month_pnl) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", (now, a, coin, kind, side, value, price, lev, perf(r, "month")))
                if value >= MIN_ALERT:
                    alerts.append(alert_line(a, perf(r, "month"), kind, coin, side, value, lev, price))
        else:
            conn.execute("INSERT INTO whale_seen VALUES (?,?)", (a, now))
        conn.execute("DELETE FROM whale_state WHERE addr=?", (a,))
        conn.executemany("INSERT INTO whale_state VALUES (?,?,?,?,?,?)",
                         [(a, c, p["szi"], p["value"], p["entry"], p["lev"]) for c, p in cur.items()])
        conn.commit()
        time.sleep(0.1)
    return alerts


def consensus(conn, top=10):
    """Per coin: $ long vs $ short across tracked whales, and how many of each."""
    rows = conn.execute(
        "SELECT coin, SUM(CASE WHEN szi>0 THEN value ELSE 0 END), SUM(CASE WHEN szi<0 THEN value ELSE 0 END), "
        "SUM(szi>0), SUM(szi<0) FROM whale_state GROUP BY coin ORDER BY SUM(value) DESC LIMIT ?", (top,)).fetchall()
    return [{"coin": c, "long": l or 0, "short": s or 0, "n_long": nl, "n_short": ns} for c, l, s, nl, ns in rows]


def summary_text(conn, events=8):
    n = conn.execute("SELECT COUNT(DISTINCT addr) FROM whale_seen").fetchone()[0]
    lines = [f"🐋 <b>Киты Hyperliquid</b> · слежу за {n} кошельками (в плюсе за месяц и за всё время)"]
    cons = consensus(conn)
    if cons:
        lines.append("\n<b>Где стоят сейчас</b> (лонг / шорт, число китов):")
        for x in cons:
            tot = x["long"] + x["short"]
            bias = "🟢" if x["long"] > x["short"] * 1.5 else "🔴" if x["short"] > x["long"] * 1.5 else "⚪"
            lines.append(f"{bias} <b>{html.escape(x['coin'])}</b> ${x['long'] / 1e6:,.0f}M / ${x['short'] / 1e6:,.0f}M "
                         f"({x['n_long']}↑ {x['n_short']}↓) · {x['long'] / tot * 100:.0f}% в лонг")
    ev = conn.execute("SELECT ts, addr, coin, kind, side, value, price, lev, month_pnl FROM whale_events "
                      "WHERE value >= ? ORDER BY ts DESC LIMIT ?", (MIN_ALERT, events)).fetchall()
    if ev:
        lines.append("\n<b>Последние крупные действия:</b>")
        for ts, a, c, k, s, v, p, l, m in ev:
            lines.append(time.strftime("%d.%m %H:%M", time.gmtime(ts + 3 * 3600)) + " " +
                         alert_line(a, m or 0, k, c, s, v, l, p))
    lines.append("\n<i>Позиция кита может быть хеджем сделки на другой бирже. Это данные, не сигнал.</i>")
    return "\n".join(lines)


def loop(every):
    import telegram_notify as tn
    conn = connect()
    whales, loaded = [], 0
    while True:
        try:
            if time.time() - loaded > REFRESH_S or not whales:
                whales = pick_whales(_json(urllib.request.Request(LEADERBOARD, headers={"User-Agent": "Mozilla/5.0"}))
                                     ["leaderboardRows"])
                loaded = time.time()
            alerts = cycle(conn, whales)
            if alerts:
                tn._send("\n".join(alerts))
        except Exception as e:
            print(f"whales cycle error: {e!r}", file=sys.stderr, flush=True)
        time.sleep(every)


def selftest():
    P = lambda szi, value, lev=5: {"szi": szi, "value": value, "entry": 1.0, "lev": lev}
    ev = diff({"BTC": P(1, 10e6), "ETH": P(2, 8e6), "SOL": P(-5, 3e6), "HYPE": P(4, 6e6)},
              {"BTC": P(1.1, 11e6), "ETH": P(-2, 8e6), "SOL": P(-10, 6e6), "HYPE": P(1, 1.5e6), "ZEC": P(3, 7e6)})
    got = {(k, c, s) for k, c, s, _ in ev}
    assert got == {("flip", "ETH", "short"), ("add", "SOL", "short"), ("cut", "HYPE", "long"),
                   ("open", "ZEC", "long")}, got          # BTC +10% is not an event
    assert ("close", "BTC", "long") in {(k, c, s) for k, c, s, _ in diff({"BTC": P(1, 1e6)}, {})}
    add = next(v for k, c, s, v in ev if k == "add")
    assert add == 3e6                                      # add carries the change, not the total

    rows = [{"ethAddress": "0xa", "accountValue": "5e6", "windowPerformances": [["month", {"pnl": "9"}], ["allTime", {"pnl": "1"}]]},
            {"ethAddress": "0xvault", "accountValue": "9e9", "windowPerformances": [["month", {"pnl": "99"}], ["allTime", {"pnl": "9"}]]},
            {"ethAddress": "0xloser", "accountValue": "5e6", "windowPerformances": [["month", {"pnl": "50"}], ["allTime", {"pnl": "-5"}]]}]
    assert [r["ethAddress"] for r in pick_whales(rows)] == ["0xa"]

    conn = connect(":memory:")
    w = [dict(rows[0], ethAddress="0x" + "1" * 40)]
    snap = {"BTC": P(1, 20e6, 10)}
    assert cycle(conn, w, now=1, fetch=lambda a: snap, mids={"BTC": 80000.0}) == []  # baseline: silent
    snap = {"BTC": P(1, 20e6, 10), "ETH": P(-3, 9e6, 5), "DOGE": P(1, 6e5)}
    alerts = cycle(conn, w, now=2, fetch=lambda a: snap, mids={"ETH": 4000.0, "DOGE": 0.1})
    assert len(alerts) == 1 and "ETH" in alerts[0] and "ШОРТ" in alerts[0] and "открыл" in alerts[0], alerts
    assert conn.execute("SELECT COUNT(*) FROM whale_events").fetchone()[0] == 2  # small DOGE logged, not alerted
    cons = {x["coin"]: x for x in consensus(conn)}
    assert cons["BTC"]["long"] == 20e6 and cons["ETH"]["short"] == 9e6
    t = summary_text(conn)
    assert "Где стоят" in t and "ETH" in t and "хеджем" in t
    print("whales selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", type=int, metavar="SECONDS")
    p.add_argument("--consensus", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        selftest()
    elif a.loop:
        loop(a.loop)
    elif a.once:
        c = connect()
        board = _json(urllib.request.Request(LEADERBOARD, headers={"User-Agent": "Mozilla/5.0"}))["leaderboardRows"]
        for line in cycle(c, pick_whales(board)):
            print(line)
        print(summary_text(c))
    elif a.consensus:
        print(summary_text(connect()))
    else:
        sys.exit(__doc__)

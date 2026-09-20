"""SQLite point-in-time log, opt-in via --log on scout.py/scalp.py.

This is the single biggest gap a real quant would flag in this repo: every
candidate rule (RSI overbought/oversold, funding z-score threshold) was
written by intuition and has never been checked against what actually
happened afterward. You cannot validate a signal you never logged. This
file is the plumbing for that — not a finished backtest (see validate.py
for why a real one needs weeks of accumulated data this repo doesn't have
yet), just the part that makes accumulating it possible starting today.

Opt-in, not automatic: a screening tool shouldn't silently write to disk
on every run unless asked (`--log`).
"""
import sqlite3, time
from pathlib import Path

DB_PATH = Path(__file__).parent / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scout_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    price REAL, pct_24h REAL, pct_7d REAL,
    rsi REAL, stoch REAL, signal TEXT, news_score REAL
);
CREATE INDEX IF NOT EXISTS idx_scout_symbol_ts ON scout_runs(symbol, ts);

CREATE TABLE IF NOT EXISTS scalp_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    funding_z REAL, rsi_1m REAL, stoch_1m REAL, candidate INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scalp_symbol_ts ON scalp_runs(symbol, ts);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def log_scout(ranked, conn=None):
    """ranked — the list scout.py's rank() returns. One row per coin, per run."""
    own = conn is None
    conn = conn or connect()
    ts = int(time.time())
    conn.executemany(
        "INSERT INTO scout_runs (ts, symbol, price, pct_24h, pct_7d, rsi, stoch, signal, news_score) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(ts, c["symbol"].upper(), c.get("current_price"), c.get("pct_24h"), c.get("pct_7d"),
          (c.get("osc") or {}).get("rsi"), (c.get("osc") or {}).get("stoch"),
          (c.get("osc") or {}).get("signal"), c.get("news_score")) for c in ranked])
    conn.commit()
    if own:
        conn.close()


def log_scalp(results, conn=None):
    own = conn is None
    conn = conn or connect()
    ts = int(time.time())
    rows = [(ts, r["symbol"], r["funding"].get("z"), r["osc"].get("rsi"),
              r["osc"].get("stoch"), int(r["candidate"]))
            for r in results if "error" not in r]
    conn.executemany(
        "INSERT INTO scalp_runs (ts, symbol, funding_z, rsi_1m, stoch_1m, candidate) VALUES (?,?,?,?,?,?)",
        rows)
    conn.commit()
    if own:
        conn.close()


def selftest():
    conn = connect(":memory:")
    log_scout([{"symbol": "btc", "current_price": 100, "pct_24h": 1.0, "pct_7d": 2.0,
                "osc": {"rsi": 25, "stoch": 10, "signal": "oversold"}, "news_score": 0.5}], conn=conn)
    rows = conn.execute("SELECT symbol, signal FROM scout_runs").fetchall()
    assert rows == [("BTC", "oversold")], rows

    log_scalp([{"symbol": "ETHUSDT", "funding": {"z": 2.1}, "osc": {"rsi": 71, "stoch": 88},
                "candidate": True},
               {"symbol": "BAD", "error": "network"}], conn=conn)
    rows = conn.execute("SELECT symbol, candidate FROM scalp_runs").fetchall()
    assert rows == [("ETHUSDT", 1)], rows  # the errored symbol never got a row
    conn.close()
    print("history selftest ok")


if __name__ == "__main__":
    selftest()

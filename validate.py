"""Reads history.db and asks the one question this repo could not honestly
answer before it existed: after a coin was flagged oversold/overbought, or
a scalp candidate fired, what actually happened to its price N hours later?

This is NOT a finished backtest, and it says so loudly on purpose. A real
backtest needs weeks-to-months of point-in-time data across many coins;
CoinGecko's free OHLC only goes back in coarse multi-day candles and
Bybit's funding history has been intermittently geo-blocked from this
session's network (see scalp.py's BybitGeoBlocked). Reconstructing history
retroactively from those free, rate-limited APIs and calling it a backtest
would be exactly the kind of overconfident, unvalidated claim this whole
exercise is supposed to avoid. The honest path is: log every real run
(`--log` on scout.py/scalp.py) starting today, and only trust this script's
output once it has real weeks of accumulated data to work with.

    python3 validate.py                 # reads history.db, reports what it can
    python3 validate.py --min-samples 30  # raise the bar before printing a verdict
"""
import argparse
import history


def forward_move(conn, symbol, after_ts, horizon_seconds, table="scout_runs", price_col="price"):
    """The next logged price for this symbol at least `horizon_seconds` after
    `after_ts` — None if nothing that far ahead has been logged yet."""
    row = conn.execute(
        f"SELECT {price_col} FROM {table} WHERE symbol=? AND ts>=? ORDER BY ts ASC LIMIT 1",
        (symbol, after_ts + horizon_seconds)).fetchone()
    return row[0] if row else None


def evaluate_scout_signal(conn, horizon_hours=24):
    """For every logged 'oversold'/'overbought' reading, find its actual
    forward price move once enough time has passed. Returns per-signal
    sample counts and mean forward % move — the raw material for a verdict,
    not a verdict by itself (see min-samples gate in __main__)."""
    rows = conn.execute(
        "SELECT symbol, ts, price, signal FROM scout_runs WHERE signal IN ('oversold','overbought')").fetchall()
    out = {"oversold": [], "overbought": []}
    for symbol, ts, price, signal in rows:
        if not price:
            continue
        fwd = forward_move(conn, symbol, ts, horizon_hours * 3600)
        if fwd is not None:
            out[signal].append((fwd - price) / price * 100)
    return out


def report(moves, horizon_hours, min_samples):
    for signal, values in moves.items():
        n = len(values)
        if n < min_samples:
            print(f"{signal:>12}: n={n} — below --min-samples ({min_samples}), no verdict printed. "
                  f"Keep logging runs (`--log`) and re-run this later.")
            continue
        mean = sum(values) / n
        print(f"{signal:>12}: n={n}  mean {horizon_hours}h forward move: {mean:+.2f}%")
    print("\nA mean move alone is not significance — with real sample size, follow up with")
    print("a proper test (e.g. compare against the unconditional mean move for all coins,")
    print("same period) before trusting this as an edge. This script gives you the raw")
    print("numbers; it does not run that test for you.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--horizon-hours", type=int, default=24)
    p.add_argument("--min-samples", type=int, default=20,
                    help="don't print a verdict for a signal with fewer logged instances than this")
    args = p.parse_args()

    conn = history.connect()
    total = conn.execute("SELECT COUNT(*) FROM scout_runs").fetchone()[0]
    if total == 0:
        print("history.db is empty. Run scout.py/scalp.py with --log a few times "
              "(ideally on a schedule, over days-to-weeks) before this has anything to say.")
    else:
        print(f"{total} logged scout rows so far.")
        report(evaluate_scout_signal(conn, args.horizon_hours), args.horizon_hours, args.min_samples)
    conn.close()

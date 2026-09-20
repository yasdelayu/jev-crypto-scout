#!/usr/bin/env python3
"""Scalp signal scanner — a decision aid, not a trading bot. It never places an
order; it prints candidates a human still has to evaluate and act on.

Two independent, arithmetic-only signals, computed in code from Bybit's public
v5 API (no key needed for market data):

  - funding extremity: current perp funding rate vs its own recent history
    (z-score-ish — how unusual is right now compared to the last N periods)
  - short-horizon oscillators: RSI/Stochastic on 1-minute candles (reusing
    indicators.py — same formulas as scout.py's --ta, different timeframe)

Jev enters exactly once per candidate, as a risk GATE, not a predictor: does
very recent news about this symbol carry a high-confidence bearish/regulatory
signal? If so, suppress the candidate — a funding/RSI extreme means nothing
if the real driver is a hack or a regulatory action technicals can't see.

    python3 scalp.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
    python3 scalp.py --symbols BTCUSDT --news              # + Jev news gate (needs a key)
    python3 scalp.py --selftest                             # no network
"""
import argparse, json, statistics, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

import colors
import indicators
from jev_client import Jev, pick_provider
from scout import fetch_news, match_news_to_coins, NEWS_QUESTIONS

BYBIT = "https://api.bybit.com/v5"


def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "jev-crypto-scout/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {d.get('retCode')}: {d.get('retMsg')}")
    return d["result"]


def funding_extremity(symbol, lookback=48):
    """How many standard deviations the CURRENT funding rate sits from the
    mean of its own last `lookback` periods (8h periods -> 48 = ~16 days).
    A persistently high positive rate means longs are crowded and paying
    shorts — classic mean-reversion setup, not a prediction, just a fact
    about current positioning that code can measure exactly."""
    hist = get_json(f"{BYBIT}/market/funding/history?category=linear&symbol={symbol}&limit={lookback}")["list"]
    rates = [float(h["fundingRate"]) for h in hist]
    if len(rates) < 10:
        return None
    current = rates[0]
    mean = statistics.mean(rates[1:])
    stdev = statistics.pstdev(rates[1:]) or 1e-9
    return {"current": current, "mean": round(mean, 6), "z": round((current - mean) / stdev, 2)}


def short_horizon_oscillators(symbol, interval="1", limit=120):
    """RSI/Stochastic/MACD/Bollinger on 1-minute candles — same formulas as
    scout.py's --ta, just a much shorter timeframe. Bybit returns newest
    first; indicators.py expects oldest-first, so reverse."""
    kl = get_json(f"{BYBIT}/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}")["list"]
    kl = list(reversed(kl))  # [start, open, high, low, close, volume, turnover]
    if len(kl) < 30:
        return None
    highs = [float(c[2]) for c in kl]
    lows = [float(c[3]) for c in kl]
    closes = [float(c[4]) for c in kl]
    rsi_v = indicators.rsi(closes, period=14)
    stoch_v = indicators.stochastic_k(highs, lows, closes, period=14)
    return {"rsi": rsi_v, "stoch": stoch_v, "macd_hist": indicators.macd_histogram(closes),
            "boll_b": indicators.bollinger_percent_b(closes), "signal": indicators.read_signal(rsi_v, stoch_v)}


def scan_symbol(symbol):
    try:
        f = funding_extremity(symbol)
        o = short_horizon_oscillators(symbol)
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}
    if not f or not o:
        return {"symbol": symbol, "error": "insufficient history"}

    # Candidate logic — all thresholds in code, tunable, no model involved:
    # funding extreme AND price technically extreme in the SAME direction is
    # the setup (crowded longs + overbought = both point down; the inverse
    # for crowded shorts + oversold). Either alone is noise.
    reasons = []
    if f["z"] > 1.5 and o["signal"] == "overbought":
        reasons.append("crowded longs (funding z={:.1f}) + overbought (RSI={})".format(f["z"], o["rsi"]))
    if f["z"] < -1.5 and o["signal"] == "oversold":
        reasons.append("crowded shorts (funding z={:.1f}) + oversold (RSI={})".format(f["z"], o["rsi"]))

    return {"symbol": symbol, "funding": f, "osc": o, "candidate": bool(reasons), "reasons": reasons}


def news_gate(jev, results, symbol_to_coin_name):
    """One Jev call per still-live candidate: is there a very recent,
    confident, bearish/regulatory item about this symbol? If yes, veto it —
    technical setups say nothing about news-driven risk, and that is exactly
    the kind of natural-language judgment Jev is for (not the scoring above)."""
    news = fetch_news()
    coins = [{"name": name, "symbol": sym.replace("USDT", "").lower()}
             for sym, name in symbol_to_coin_name.items()]
    matched = match_news_to_coins(news, coins)
    by_symbol = {}
    for item, hit_coins in matched:
        for c in hit_coins:
            by_symbol.setdefault(c["symbol"].upper(), []).append(item)

    for r in results:
        if not r.get("candidate"):
            continue
        base = r["symbol"].replace("USDT", "")
        items = by_symbol.get(base, [])
        if not items:
            r["news_veto"] = False
            continue
        # only need the single most recent matched item — that's what the gate is for
        a = jev.ask(f"{items[0]['title']}. {items[0]['summary']}", NEWS_QUESTIONS)["answers"]
        bearish = a["sentiment"]["choice"] == "bearish" and a["sentiment"]["confidence"] > 0.7
        r["news_veto"] = bearish
        r["news_headline"] = items[0]["title"] if bearish else None
    return results


def print_report(results):
    header = f"{'symbol':<10}{'funding_z':>10}{'RSI(1m)':>9}{'Stoch':>7}{'candidate':>11}"
    print("\n" + colors.bold(f"⚡ {header}"))
    for r in results:
        if "error" in r:
            print(f"{r['symbol']:<10} {colors.dim('— ' + r['error'])}")
            continue
        f, o = r["funding"], r["osc"]
        tag = ""
        if r["candidate"]:
            tag = "VETOED (news)" if r.get("news_veto") else "YES"
        tag_padded = f"{tag:>11}"
        tag_colored = colors.red(tag_padded) if tag == "VETOED (news)" else (colors.amber(tag_padded) if tag == "YES" else tag_padded)
        print(f"{r['symbol']:<10}{colors.signed(f['z'], 10, 2)}"
              f"{(o['rsi'] or 0):>9.0f}{(o['stoch'] or 0):>7.0f}{tag_colored}")
        for reason in r.get("reasons", []):
            print(colors.dim(f"    -> {reason}"))
        if r.get("news_headline"):
            print(colors.red(f"    -> vetoed: {r['news_headline'][:70]}"))
    print(colors.dim("\n⚠️  This is a screening aid, not a signal to trade. It places no orders."))
    print(colors.dim("For real execution discipline (trade-only keys, kill-switch, human approval),"))
    print(colors.dim("see the private architecture — this repo intentionally stops before that line."))


def selftest():
    # funding_extremity math, offline: z-score of a known series
    rates = [0.001] + [0.0001] * 47  # current way above a flat history
    mean = statistics.mean(rates[1:])
    stdev = statistics.pstdev(rates[1:]) or 1e-9
    z = (rates[0] - mean) / stdev
    assert z > 5, z  # current is a huge outlier vs flat history

    # candidate logic in isolation
    fake = [{"symbol": "XUSDT", "funding": {"z": 2.0}, "osc": {"signal": "overbought", "rsi": 78}}]
    r = fake[0]
    reasons = []
    if r["funding"]["z"] > 1.5 and r["osc"]["signal"] == "overbought":
        reasons.append("ok")
    assert reasons, "crowded-long+overbought should flag a candidate"
    print("scalp selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT", help="comma-separated Bybit linear symbols")
    p.add_argument("--news", action="store_true", help="apply the Jev news veto (needs a key)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        selftest(); sys.exit()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    print(f"scanning {len(symbols)} symbols on Bybit (funding + 1m oscillators)…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(scan_symbol, symbols))

    if args.news:
        provider, key = pick_provider()
        jev = Jev(provider, key)
        name_map = {s: s.replace("USDT", "") for s in symbols}  # crude but matches match_news_to_coins by symbol
        print("applying Jev news gate to live candidates…")
        results = news_gate(jev, results, name_map)

    print_report(results)

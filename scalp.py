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

Note on `HTTP Error 403: Forbidden`: that's Bybit's CloudFront/WAF layer
temporarily blocking bursty traffic from one IP — not this script's fault,
not a permanent geo-block (unlike Binance's real 451 elsewhere in this repo).
It clears on its own after a short cooldown; re-running a minute later works.
Firing this and scout.py's --scalp back-to-back many times in a short window
is exactly what triggers it — space runs out if you're iterating fast.
"""
import argparse, json, statistics, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

import colors
import indicators
from jev_client import Jev, pick_provider
from http_client import get_json as _get_json_raw
from scout import fetch_news, match_news_to_coins, NEWS_QUESTIONS_BY_LANG

BYBIT = "https://api.bybit.com/v5"

# Bybit's own "geo-blocked" message doesn't come through as a normal HTTPError —
# their CloudFront front door hands back a non-JSON body reading literally
# "The Amazon CloudFront distribution is configured to block access from your
# country" with a 200/403 depending on the route. Seen live 21.09.2026 on
# funding/history specifically while tickers/kline/instruments-info kept
# working — so this is scoped to one distribution, not all of Bybit, and it
# comes and goes with routing, not a fixed rule. Surfacing it as its own
# exception type lets callers degrade gracefully instead of crashing the scan.
class BybitGeoBlocked(RuntimeError):
    pass


def get_json(url, timeout=15):
    """Bybit-flavored JSON GET: shared retry/backoff from http_client, plus
    Bybit's own retCode envelope check and the CloudFront geo-block above."""
    try:
        d = _get_json_raw(url, timeout=timeout)
    except json.JSONDecodeError:
        raise BybitGeoBlocked("CloudFront blocked this endpoint from the current network/region")
    if not isinstance(d, dict) or "retCode" not in d:
        raise BybitGeoBlocked("unexpected response shape — likely the same CloudFront block")
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {d.get('retCode')}: {d.get('retMsg')}")
    return d["result"]


def fetch_instruments():
    """One global call, not per symbol: which linear pairs actually exist on
    Bybit, and each one's real settlement interval (`fundingInterval`, in
    minutes — usually 480 = 8h, but the API exposes it per symbol instead of
    a fixed global constant, so read it rather than hardcode 8h everywhere).
    Fetched once per run and reused — this is what replaces catching a
    'Symbol Invalid' error per coin with a clean skip before ever asking.

    Returns None on failure (network/Bybit's own CloudFront front door can
    block THIS endpoint specifically while others keep working — seen live)
    rather than raising: this call gates the whole run in both __main__
    blocks that use it, and a single blocked call here shouldn't take down
    a scan that would otherwise have worked fine per-symbol."""
    try:
        rows = get_json(f"{BYBIT}/market/instruments-info?category=linear")["list"]
    except Exception:
        return None
    return {r["symbol"]: {"funding_interval_min": int(r["fundingInterval"])} for r in rows}


def fetch_ticker(symbol):
    """Live funding rate and the exact timestamp of the next settlement —
    both on the tickers endpoint, no extra call needed for either."""
    t = get_json(f"{BYBIT}/market/tickers?category=linear&symbol={symbol}")["list"][0]
    return {"rate": float(t["fundingRate"]), "next_funding_ms": int(t["nextFundingTime"])}


def funding_extremity(symbol, interval_min, lookback=48):
    """Not just a z-score — the actual fact, in the terms a trader thinks in:
    who is paying whom right now, how many settlements in a row it's held
    that way, and how long until the next one resets the clock. The z-score
    (how many standard deviations from its own recent mean) still drives the
    candidate logic in scan_symbol(); everything else here is for a human
    reading the line, which a bare number never explains on its own.

    Degrades instead of failing when the history leg alone is geo-blocked
    (see BybitGeoBlocked above): tickers/kline kept working the whole time
    this was observed, so direction and next-reset are still real — only the
    z-score and streak (which need the history) go missing, honestly, not
    silently faked as zero."""
    ticker = fetch_ticker(symbol)
    current = ticker["rate"]
    next_reset_min = max(0, round((ticker["next_funding_ms"] - time.time() * 1000) / 60000))
    sign = (current > 0) - (current < 0)  # +1 / -1 / 0
    direction = {1: "лонги платят шортам", -1: "шорты платят лонгам", 0: "нейтрально"}[sign]

    try:
        hist = get_json(f"{BYBIT}/market/funding/history?category=linear&symbol={symbol}&limit={lookback}")["list"]
    except BybitGeoBlocked:
        return {"current": current, "z": None, "direction": direction, "streak_periods": None,
                "interval_hours": interval_min / 60, "next_reset_min": next_reset_min,
                "history_unavailable": True}

    rates = [float(h["fundingRate"]) for h in hist]
    if len(rates) < 10:
        return None
    mean = statistics.mean(rates[1:])
    stdev = statistics.pstdev(rates[1:]) or 1e-9
    streak = 0
    for r in rates:
        if ((r > 0) - (r < 0)) != sign:
            break
        streak += 1

    return {"current": current, "mean": round(mean, 6), "z": round((current - mean) / stdev, 2),
            "direction": direction, "streak_periods": streak,
            "interval_hours": interval_min / 60, "next_reset_min": next_reset_min,
            "history_unavailable": False}


def format_funding_note(f):
    """One readable line: direction, how long it's held, when it resets —
    the answer to 'фандинг минута — а кто кому платит, как давно, когда конец'."""
    m = f["next_reset_min"]
    reset = f"{m // 60}ч{m % 60:02d}м" if m >= 60 else f"{m}м"
    if f.get("history_unavailable"):
        return f"{f['direction']} · история недоступна (Bybit geo-block) · сброс через {reset}"
    return f"{f['direction']} · {f['streak_periods']}×{f['interval_hours']:.0f}ч подряд · сброс через {reset}"


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


def scan_symbol(symbol, interval_min=480):
    try:
        f = funding_extremity(symbol, interval_min)
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
    if f["z"] is not None:  # None means Bybit's funding-history leg was geo-blocked this run —
        if f["z"] > 1.5 and o["signal"] == "overbought":  # can't call a candidate without the z-score
            reasons.append("crowded longs (funding z={:.1f}) + overbought (RSI={})".format(f["z"], o["rsi"]))
        if f["z"] < -1.5 and o["signal"] == "oversold":
            reasons.append("crowded shorts (funding z={:.1f}) + oversold (RSI={})".format(f["z"], o["rsi"]))

    return {"symbol": symbol, "funding": f, "osc": o, "candidate": bool(reasons), "reasons": reasons}


def news_gate(jev, results, symbol_to_coin_name, lang="en"):
    """One Jev call per still-live candidate: is there a very recent,
    confident, bearish/regulatory item about this symbol? If yes, veto it —
    technical setups say nothing about news-driven risk, and that is exactly
    the kind of natural-language judgment Jev is for (not the scoring above)."""
    questions = NEWS_QUESTIONS_BY_LANG[lang]
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
        a = jev.ask(f"{items[0]['title']}. {items[0]['summary']}", questions)["answers"]
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
        print(colors.dim(f"    {format_funding_note(f)}"))
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

    # streak counting: leading run of same-sign entries, offline
    def streak_of(rates, sign):
        n = 0
        for r in rates:
            if ((r > 0) - (r < 0)) != sign:
                break
            n += 1
        return n
    assert streak_of([0.1, 0.2, 0.3, -0.1], 1) == 3
    assert streak_of([-0.1, -0.2, 0.3], -1) == 2
    assert streak_of([0.0, 0.1], 0) == 1  # zero breaks a positive streak immediately

    # human-readable note formats without crashing on edge values
    note = format_funding_note({"direction": "лонги платят шортам", "streak_periods": 3,
                                 "interval_hours": 8.0, "next_reset_min": 222})  # 222м = 3ч42м
    assert "сброс через 3ч42м" in note, note
    note0 = format_funding_note({"direction": "нейтрально", "streak_periods": 0,
                                  "interval_hours": 8.0, "next_reset_min": 5})
    assert "сброс через 5м" in note0, note0

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
    p.add_argument("--lang", choices=["en", "ru"], default="en", help="language of the news questions sent to Jev")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        selftest(); sys.exit()

    requested = [s.strip().upper() for s in args.symbols.split(",")]
    instruments = fetch_instruments()
    if instruments is None:
        print(colors.dim("не удалось получить список инструментов Bybit (сеть/блок) — "
                          "пропускаю предфильтр, пробую все запрошенные символы как есть"))
        symbols, skipped, instruments = requested, [], {}
    else:
        symbols = [s for s in requested if s in instruments]
        skipped = [s for s in requested if s not in instruments]
        if skipped:
            print(colors.dim(f"пропускаю (не торгуется на Bybit как перпетуум): {', '.join(skipped)}"))

    print(f"scanning {len(symbols)} symbols on Bybit (funding + 1m oscillators)…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(
            lambda s: scan_symbol(s, instruments.get(s, {}).get("funding_interval_min", 480)), symbols))

    if args.news:
        provider, key = pick_provider()
        jev = Jev(provider, key)
        name_map = {s: s.replace("USDT", "") for s in symbols}  # crude but matches match_news_to_coins by symbol
        print("applying Jev news gate to live candidates…")
        results = news_gate(jev, results, name_map, lang=args.lang)

    print_report(results)

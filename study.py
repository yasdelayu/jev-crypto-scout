#!/usr/bin/env python3
"""Research on Bybit history — turning a trader's intuitions into numbers.

    python3 study.py sessions   # what each UTC hour / trading session does, and
                                # whether the US session reverses Asia
    python3 study.py funding    # price around extreme funding settlements:
                                # T−30m → T → T+5/30/60m, betting against the payers
    python3 study.py settlement # robustness of the post-settlement move: more history,
                                # entry delay, exits, fees+slippage, splits, halves
    python3 study.py ticks      # same on tick data (public.bybit.com): entry +0.25s…+30s
    python3 study.py fresh      # funding JUST turned anomalous (premium index, 5m): enter,
                                # exit before the payout — with the crowd vs against it
    python3 study.py spikes     # fresh ≥0.5% spikes over 90d: hold 1–12h WITH the crowd,
                                # funding actually paid counted; how long anomalies last
    python3 study.py btc        # BTC daily since Oct 2024: key dates, biggest days, months
    python3 study.py --selftest

Only public market data, no keys. Run from a network Bybit doesn't geo-block.
"""
import statistics, sys, time

import scalp

MSK = 3  # UTC+3


def klines(sym, interval, start_ms, end_ms, limit=1000, kind="kline"):
    """[(ts_ms, open, high, low, close)] oldest-first, paging backwards.
    kind="premium-index-price-kline" gives the premium index (what the next
    funding rate is computed from) in the same shape."""
    out, end = [], end_ms
    while end > start_ms:
        rows = scalp.get_json(f"{scalp.BYBIT}/market/{kind}?category=linear&symbol={sym}"
                              f"&interval={interval}&start={start_ms}&end={end}&limit={limit}")["list"]
        if not rows:
            break
        out += [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in rows]
        if len(rows) < limit:
            break
        end = int(rows[-1][0]) - 1
        time.sleep(0.1)
    return sorted(set(out))


def tstat(xs):
    if len(xs) < 3 or statistics.pstdev(xs) == 0:
        return 0.0
    return statistics.mean(xs) / (statistics.stdev(xs) / len(xs) ** 0.5)


def stats_line(xs):
    return (f"n={len(xs):>4}  среднее {statistics.mean(xs):+.3f}%  "
            f"в плюс {sum(x > 0 for x in xs) / len(xs) * 100:>3.0f}%  t={tstat(xs):+.1f}") if xs else "n=0"


# ---------- сессии ----------

SESSIONS = [("Азия (Токио)", 0, 7), ("Европа (Лондон)", 7, 13), ("Америка (Нью-Йорк)", 13, 20), ("ночь США", 20, 24)]


def session_of(hour):
    return next(name for name, a, b in SESSIONS if a <= hour < b)


def sessions(coins=("BTCUSDT", "ETHUSDT", "SOLUSDT"), days=365):
    now = int(time.time() * 1000)
    for sym in coins:
        k = klines(sym, "60", now - days * 86_400_000, now)
        by_hour = {h: [] for h in range(24)}
        days_map = {}
        for ts, o, h, l, c in k:
            g = time.gmtime(ts / 1000)
            r = (c - o) / o * 100
            by_hour[g.tm_hour].append((r, (h - l) / o * 100))
            d = days_map.setdefault((g.tm_year, g.tm_yday), {n: 0.0 for n, _, _ in SESSIONS})
            d[session_of(g.tm_hour)] += r
        print(f"\n===== {sym}: {len(k)} часовых свечей за {days} дн. =====")
        print("час UTC (МСК)   среднее    в плюс   размах ч   t")
        for hr in range(24):
            rs = [x[0] for x in by_hour[hr]]
            rng = statistics.mean(x[1] for x in by_hour[hr])
            mark = "  <-- " if abs(tstat(rs)) >= 2 else ""
            print(f"  {hr:02d} ({(hr + MSK) % 24:02d})     {statistics.mean(rs):+.3f}%   "
                  f"{sum(x > 0 for x in rs) / len(rs) * 100:>3.0f}%    {rng:.2f}%   {tstat(rs):+.1f}{mark}")
        full = [d for d in days_map.values()]
        for name, _, _ in SESSIONS:
            print(f"  {name:<20} {stats_line([d[name] for d in full])}")
        a = [d["Азия (Токио)"] for d in full]
        e = [d["Европа (Лондон)"] for d in full]
        u = [d["Америка (Нью-Йорк)"] for d in full]
        print(f"  корреляция Азия→Америка {statistics.correlation(a, u):+.2f} · "
              f"Европа→Америка {statistics.correlation(e, u):+.2f}  (минус = Америка разворачивает)")
        # Америка против Азии, когда Азия сходила сильно
        big = [(x, y) for x, y in zip(a, u) if abs(x) >= 2]
        if big:
            rev = sum(1 for x, y in big if x * y < 0)
            print(f"  дни с Азией ≥±2% ({len(big)}): Америка пошла против Азии в {rev / len(big) * 100:.0f}% случаев")


# ---------- фандинг у расчёта ----------

def funding_events(min_abs=0.002, min_turnover=5e6):
    tick = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
    syms = [t["symbol"] for t in tick
            if t["symbol"].endswith("USDT") and float(t.get("turnover24h") or 0) >= min_turnover]
    ev = []
    for s in syms:
        try:
            h = scalp.get_json(f"{scalp.BYBIT}/market/funding/history?category=linear&symbol={s}&limit=200")["list"]
        except Exception:
            continue
        ev += [(s, int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in h
               if abs(float(x["fundingRate"])) >= min_abs]
        time.sleep(0.05)
    return ev, len(syms)


def event_moves(k, T, rate):
    """Signed % moves around settlement T, betting AGAINST the payers
    (rate<0 → shorts pay → bet up). Opens of 1m candles at T−30, T, T+5, T+30, T+60."""
    px = {ts: o for ts, o, *_ in k}
    need = {m: T + m * 60_000 for m in (-30, 0, 5, 30, 60)}
    if not all(v in px for v in need.values()):
        return None
    p = {m: px[v] for m, v in need.items()}
    d = 1 if rate < 0 else -1
    mv = lambda a, b: d * (p[b] - p[a]) / p[a] * 100
    # «сделка»: вошёл за 30 мин по направлению против плательщиков, получил фандинг в T, вышел в T+5
    fee = 0.11  # taker вход+выход, %
    return {"pre": mv(-30, 0), "post5": mv(0, 5), "post30": mv(0, 30), "post60": mv(0, 60),
            "trade": mv(-30, 5) + abs(rate) * 100 - fee}


def funding(min_abs=0.002):
    ev, n_syms = funding_events(min_abs)
    print(f"\nперпов с оборотом ≥$5M: {n_syms}; расчётов с |ставкой| ≥ {min_abs * 100:.1f}%: {len(ev)}")
    res = []
    for s, T, r in ev:
        try:
            k = klines(s, "1", T - 3_600_000, T + 3_700_000, limit=200)
        except Exception:
            continue
        m = event_moves(k, T, r)
        if m:
            res.append((abs(r), m))
        time.sleep(0.05)
    buckets = [("0.2–0.5%", 0.002, 0.005), ("0.5–1%", 0.005, 0.01), ("≥1%", 0.01, 9)]
    for label, lo, hi in [("ВСЕ", 0, 9)] + buckets:
        sub = [m for a, m in res if lo <= a < hi]
        if not sub:
            continue
        print(f"\n|ставка| {label}  ({len(sub)} событий) — ставим ПРОТИВ тех, кто платит:")
        for key, name in (("pre", "за 30 мин ДО сброса"), ("post5", "5 мин ПОСЛЕ"),
                          ("post30", "30 мин после"), ("post60", "60 мин после"),
                          ("trade", "СДЕЛКА: вход T−30, фандинг, выход T+5, минус комиссии")):
            print(f"  {name:<52} {stats_line([m[key] for m in sub])}")


def funding_history(sym, pages=3):
    """Up to pages×200 past settlements, newest-first pages walked backwards."""
    out, end = [], None
    for _ in range(pages):
        url = f"{scalp.BYBIT}/market/funding/history?category=linear&symbol={sym}&limit=200"
        rows = scalp.get_json(url + (f"&endTime={end}" if end else ""))["list"]
        out += [(int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in rows]
        if len(rows) < 200:
            break
        end = int(rows[-1]["fundingRateTimestamp"]) - 1
        time.sleep(0.05)
    return out


COST = 0.21  # % за сделку: taker 0.055%×2 + проскальзывание 0.05%×2


def with_payers(k, T, rate, delay, hold):
    """Trade WITH the side that paid, entered `delay` min after settlement T,
    closed `hold` min later. Returns (net %, worst adverse move %) or None."""
    by = {ts: (o, h, l, c) for ts, o, h, l, c in k}
    t0, t1 = T + delay * 60_000, T + (delay + hold) * 60_000
    if t0 not in by or t1 not in by:
        return None
    d = 1 if rate > 0 else -1  # longs paid → go long
    e, x = by[t0][0], by[t1][0]
    bars = [by[t] for t in range(t0, t1, 60_000) if t in by]
    worst = min((l - e) / e for _, _, l, _ in bars) if d > 0 else min((e - h) / e for _, h, _, _ in bars)
    return d * (x - e) / e * 100 - COST, worst * 100


def settlement(min_abs=0.001, pages=3):
    tick = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
    syms = [t["symbol"] for t in tick if t["symbol"].endswith("USDT") and float(t.get("turnover24h") or 0) >= 5e6]
    ev = []
    for s in syms:
        try:
            ev += [(s, T, r) for T, r in funding_history(s, pages) if abs(r) >= min_abs]
        except Exception:
            pass
    ev.sort(key=lambda x: x[1])
    print(f"\nперпов ≥$5M: {len(syms)}; сбросов с |ставкой| ≥ {min_abs*100:.1f}%: {len(ev)} "
          f"(с {time.strftime('%d.%m.%y', time.gmtime(ev[0][1]/1000))} по {time.strftime('%d.%m.%y', time.gmtime(ev[-1][1]/1000))})")
    rows = []
    for s, T, r in ev:
        try:
            k = klines(s, "1", T - 60_000, T + 20 * 60_000, limit=200)
        except Exception:
            continue
        rows.append((s, T, r, k))
        time.sleep(0.05)
    print(f"свечи есть для {len(rows)} событий · издержки {COST}% на сделку уже вычтены\n")
    print("ВХОД ПО СТОРОНЕ ПЛАТЕЛЬЩИКОВ после сброса (лонги платили → лонг):")
    print("вход      выход     n     среднее   в плюс    t     худший ход против (медиана / 10% хуже)")
    for delay in (0, 1):
        for hold in (2, 5, 10, 15):
            res = [x for x in (with_payers(k, T, r, delay, hold) for _, T, r, k in rows) if x]
            if not res:
                continue
            nets = [a for a, _ in res]
            worst = sorted(b for _, b in res)
            print(f"T+{delay}м    +{hold:>2}м   {len(nets):>5}   {statistics.mean(nets):+.3f}%   "
                  f"{sum(x > 0 for x in nets)/len(nets)*100:>3.0f}%   {tstat(nets):+5.1f}   "
                  f"{statistics.median(worst):+.2f}% / {worst[len(worst)//10]:+.2f}%")
    base = lambda sel: [x[0] for x in (with_payers(k, T, r, 1, 5) for s, T, r, k in rows if sel(s, T, r)) if x]
    half = rows[len(rows) // 2][1] if rows else 0
    print("\nРАЗРЕЗЫ (вход T+1м, выход +5м — реалистичный вариант):")
    for name, sel in (("лонги платили (ставка +)", lambda s, T, r: r > 0),
                      ("шорты платили (ставка −)", lambda s, T, r: r < 0),
                      ("|ставка| 0.1–0.2%", lambda s, T, r: abs(r) < 0.002),
                      ("|ставка| 0.2–0.5%", lambda s, T, r: 0.002 <= abs(r) < 0.005),
                      ("|ставка| ≥0.5%", lambda s, T, r: abs(r) >= 0.005),
                      ("первая половина истории", lambda s, T, r: T < half),
                      ("вторая половина истории", lambda s, T, r: T >= half)):
        print(f"  {name:<26} {stats_line(base(sel))}")
    top = {}
    for s, T, r, k in rows:
        x = with_payers(k, T, r, 1, 5)
        if x:
            top.setdefault(s, []).append(x[0])
    conc = sorted(((len(v), s) for s, v in top.items()), reverse=True)[:5]
    print("  чаще всего в выборке: " + ", ".join(f"{s.replace('USDT','')} ×{n}" for n, s in conc))


DELAYS = (0, 0.25, 0.5, 1, 2, 3, 5, 10, 30)  # сек после сброса
EARLY_EXITS = (1, 5, 10, 30, 60, 300)          # выход после сброса, сек (для входа заранее)


def tick_moves(trades, T, rate, exit_s=300):
    """trades: [(ts_s, price)] sorted. For each entry delay: net % of trading WITH
    the payers from the first print at/after T+delay to the first print at/after
    T+exit_s; plus the signed jump from the last pre-settlement print. None if
    the window isn't covered."""
    import bisect
    ts = [t for t, _ in trades]
    def at(x):
        i = bisect.bisect_left(ts, x)
        return trades[i][1] if i < len(trades) else None
    i0 = bisect.bisect_left(ts, T) - 1
    if i0 < 0:
        return None
    pre = trades[i0][1]
    exit_p = at(T + exit_s)
    if exit_p is None:
        return None
    d = 1 if rate > 0 else -1
    out = {"jump": {}, "net": {}, "early": {}}
    # заранее: вход по последней цене за 5 с до сброса, платим |ставку|, выходим на T+x
    j5 = bisect.bisect_right(ts, T - 5) - 1
    if j5 >= 0:
        e5 = trades[j5][1]
        for x in EARLY_EXITS:
            p = at(T + x)
            if p is None:
                return None
            out["early"][x] = d * (p - e5) / e5 * 100 - abs(rate) * 100 - COST
    for dl in DELAYS:
        e = at(T + dl)
        if e is None:
            return None
        out["jump"][dl] = d * (e - pre) / pre * 100
        out["net"][dl] = d * (exit_p - e) / e * 100 - COST
    return out


def load_ticks(sym, day, windows):
    """Stream one day's trade CSV from public.bybit.com, keep only prints inside
    the [lo, hi] windows (seconds) — the files are ~1M rows, most irrelevant."""
    import csv, gzip, io, urllib.request
    url = f"https://public.bybit.com/trading/{sym}/{sym}{day}.csv.gz"
    raw = urllib.request.urlopen(url, timeout=60).read()
    rows = []
    for r in csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), "utf-8")):
        if r[0] == "timestamp":
            continue
        t = float(r[0])
        if any(lo <= t <= hi for lo, hi in windows):
            rows.append((t, float(r[4])))
    return sorted(rows)


def settlement_ticks(min_abs=0.001, pages=3, max_files=160, per_symbol=12):
    import random
    t0 = time.time()
    scalp.get_json(f"{scalp.BYBIT}/market/time")
    print(f"задержка сервер→Bybit (HTTP запрос целиком): {(time.time() - t0) * 1000:.0f} мс")
    tick = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
    syms = [t["symbol"] for t in tick if t["symbol"].endswith("USDT") and float(t.get("turnover24h") or 0) >= 5e6]
    groups = {}
    for s in syms:
        try:
            for T, r in funding_history(s, pages):
                if abs(r) >= min_abs:
                    day = time.strftime("%Y-%m-%d", time.gmtime(T / 1000))
                    groups.setdefault((s, day), []).append((T / 1000, r))
        except Exception:
            pass
    keys = list(groups)
    random.seed(7)
    random.shuffle(keys)
    picked, per = [], {}
    for k in keys:  # не больше per_symbol дней на монету — чтобы LSK не съел выборку
        if per.get(k[0], 0) < per_symbol:
            picked.append(k)
            per[k[0]] = per.get(k[0], 0) + 1
        if len(picked) >= max_files:
            break
    res = []
    for sym, day in picked:
        evs = groups[(sym, day)]
        try:
            trades = load_ticks(sym, day, [(T - 120, T + 330) for T, _ in evs])
        except Exception:
            continue
        for T, r in evs:
            m = tick_moves(trades, T, r)
            if m:
                res.append((sym, abs(r), m))
    print(f"событий с тиками: {len(res)} ({len({s for s, _, _ in res})} монет, {len(picked)} дней-файлов) · "
          f"издержки {COST}% вычтены\n")
    print("СКАЧОК ОТ ПОСЛЕДНЕЙ ЦЕНЫ ДО СБРОСА (в сторону плательщиков) — как быстро он случается:")
    for dl in DELAYS:
        print(f"  к T+{dl:<5}с  {stats_line([m['jump'][dl] for _, _, m in res])}")
    print("\nСДЕЛКА С ПЛАТЕЛЬЩИКАМИ: вход T+задержка, выход T+5мин, после издержек:")
    for dl in DELAYS:
        print(f"  вход T+{dl:<5}с {stats_line([m['net'][dl] for _, _, m in res])}")
    for label, lo, hi in (("|ставка| 0.1–0.2%", 0, 0.002), ("|ставка| 0.2–0.5%", 0.002, 0.005), ("|ставка| ≥0.5%", 0.005, 9)):
        sub = [m for _, a, m in res if lo <= a < hi]
        if sub:
            print(f"  {label:<18} вход T+1с: {stats_line([m['net'][1] for m in sub])}")
    early = [(a, m) for _, a, m in res if m["early"]]
    print("\nЗАРАНЕЕ: вход за 5 с ДО сброса на сторону плательщиков, платим фандинг, выход T+x, после издержек:")
    for x in EARLY_EXITS:
        print(f"  выход T+{x:<4}с {stats_line([m['early'][x] for _, m in early])}")
    for label, lo, hi in (("|ставка| 0.1–0.2%", 0, 0.002), ("|ставка| 0.2–0.5%", 0.002, 0.005), ("|ставка| ≥0.5%", 0.005, 9)):
        sub = [m for a, m in early if lo <= a < hi]
        if sub:
            print(f"  {label:<18} выход T+30с: {stats_line([m['early'][30] for m in sub])}")


# ---------- свежая аномалия фандинга: вход, выход ДО выплаты ----------

FRESH_THR = (0.001, 0.002, 0.005)   # |premium index| на базе 8ч: 0.1% / 0.2% / 0.5%
FRESH_HOLDS = (3, 6, 12, 24, 48)    # баров по 5 мин: 15м, 30м, 1ч, 2ч, 4ч
BAR = 300_000


def fresh_signals(prem, thr, quiet=12):
    """Bars where |premium| just crossed thr after `quiet` bars (1h) calm below
    thr/2 — 'фандинг только что стал аномальным'. Returns [(i, sign)]."""
    out = []
    for i in range(quiet, len(prem)):
        p = prem[i][4]
        if abs(p) >= thr and max(abs(x[4]) for x in prem[i - quiet:i]) < thr / 2:
            out.append((i, 1 if p > 0 else -1))
    return out


def fresh_trades(prem, px, interval_ms, thr, hold):
    """Enter on the open of the bar AFTER the signal bar closes (no look-ahead),
    exit `hold` bars later but always before the next settlement (no funding
    paid or received). One position at a time. Returns [(with_crowd_net, ts)]."""
    opens = {ts: o for ts, o, *_ in px}
    busy, res = 0, []
    for i, sign in fresh_signals(prem, thr):
        t_in = prem[i][0] + BAR
        if t_in < busy or t_in not in opens:
            continue
        next_T = (t_in // interval_ms + 1) * interval_ms
        t_out = min(t_in + hold * BAR, next_T - BAR)   # закрыться до выплаты
        if t_out <= t_in or t_out not in opens:
            continue
        busy = t_out
        e, x = opens[t_in], opens[t_out]
        res.append((sign * (x - e) / e * 100, t_in))   # «с толпой», до издержек
    return res


def load_premium_price(days, workers=4, with_funding=False):
    """[(sym, premium_5m, price_5m, funding_history)] for every liquid USDT perp."""
    from concurrent.futures import ThreadPoolExecutor
    tick = scalp.get_json(f"{scalp.BYBIT}/market/tickers?category=linear")["list"]
    syms = [t["symbol"] for t in tick if t["symbol"].endswith("USDT") and float(t.get("turnover24h") or 0) >= 5e6]
    now = int(time.time() * 1000) // BAR * BAR
    start = now - days * 86_400_000

    def one(s):
        try:
            return (s, klines(s, "5", start, now, kind="premium-index-price-kline"), klines(s, "5", start, now),
                    funding_history(s, pages=5) if with_funding else [])
        except Exception:
            return s, None, None, None

    with ThreadPoolExecutor(workers) as pool:
        return [d for d in pool.map(one, syms) if d[1] and d[2]], len(syms)


SPIKE_H = (1, 2, 3, 6, 9, 12)


def spike_path(prem, px, fund, thr, quiet=12):
    """For each fresh spike: WITH-crowd net % at +H hours (entry next bar open),
    after costs AND the funding actually paid/received at every settlement crossed;
    plus how long the anomaly lasted (hours until |premium| < thr/2)."""
    opens = {ts: o for ts, o, *_ in px}
    out, busy = [], 0
    for i, sign in fresh_signals(prem, thr, quiet):
        t_in = prem[i][0] + BAR
        if t_in < busy or t_in not in opens:
            continue
        busy = t_in + 3 * 3_600_000
        end = next((j for j in range(i + 1, len(prem)) if abs(prem[j][4]) < thr / 2), None)
        dur = (prem[end][0] - prem[i][0]) / 3_600_000 if end is not None else None
        e = opens[t_in]
        row = {"t": t_in, "dur": dur}
        for H in SPIKE_H:
            t_out = t_in + H * 3_600_000
            if t_out not in opens:
                break
            paid = sum(sign * r * 100 for T, r in fund if t_in < T <= t_out)  # лонг платит +ставку
            row[H] = sign * (opens[t_out] - e) / e * 100 - paid - COST
        if len(row) > 2:
            out.append(row)
    return out


def spikes(days=90, thr=0.005):
    data, n = load_premium_price(days, with_funding=True)
    rows = []
    for s, prem, px, fund in data:
        rows += [dict(r, sym=s) for r in spike_path(prem, px, fund, thr)]
    print(f"\nмонет {len(data)} из {n} · {days} дн. · свежий всплеск |premium| ≥ {thr*100:.1f}% · "
          f"всплесков: {len(rows)} · вход С ТОЛПОЙ на следующем 5-мин баре")
    print("держим    (после комиссий И фактически уплаченного/полученного фандинга)")
    for H in SPIKE_H:
        print(f"  {H:>2} ч   {stats_line([r[H] for r in rows if H in r])}")
    durs = sorted(r["dur"] for r in rows if r["dur"] is not None)
    if durs:
        q = lambda p: durs[min(len(durs) - 1, int(len(durs) * p))]
        print(f"\nсколько держится аномалия (пока |premium| не упадёт вдвое): медиана {q(0.5):.1f} ч · "
              f"четверть короче {q(0.25):.1f} ч · четверть дольше {q(0.75):.1f} ч")
        for a, b in ((0, 1), (1, 3), (3, 9), (9, 999)):
            print(f"  {a}–{b if b < 999 else '∞'} ч: {sum(a <= d < b for d in durs) / len(durs) * 100:.0f}%")
    mid = sorted(r["t"] for r in rows)[len(rows) // 2] if rows else 0
    for name, sel in (("первая половина", lambda r: r["t"] < mid), ("вторая половина", lambda r: r["t"] >= mid)):
        print(f"  3 ч, {name}: {stats_line([r[3] for r in rows if sel(r) and 3 in r])}")
    top = {}
    for r in rows:
        top[r["sym"]] = top.get(r["sym"], 0) + 1
    print("  больше всего всплесков: " + ", ".join(f"{s.replace('USDT', '')} ×{c}" for s, c in
                                                  sorted(top.items(), key=lambda x: -x[1])[:8]))


def btc_timeline(start="2024-10-01"):
    """Daily BTC since `start`: key dates and the biggest daily moves."""
    import calendar
    t0 = calendar.timegm(time.strptime(start, "%Y-%m-%d")) * 1000
    k = klines("BTCUSDT", "D", t0, int(time.time() * 1000))
    day = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
    close = {day(ts): c for ts, o, h, l, c in k}
    print(f"\nBTC дневки с {start}: {len(k)} дней")
    for d, label in (("2024-11-05", "выборы в США"), ("2024-11-06", "итог выборов"), ("2024-12-17", ""),
                     ("2025-01-17", "запуск мемкоина $TRUMP"), ("2025-01-20", "инаугурация"),
                     ("2025-03-02", "пост про крипто-резерв"), ("2025-04-07", "тарифы, обвал рынков"),
                     ("2025-07-14", ""), ("2025-10-06", ""), ("2025-10-10", "пост про тарифы Китаю, ликвидации"),
                     ("2026-01-02", ""), ("2026-06-01", "")):
        if d in close:
            print(f"  {d}  ${close[d]:>9,.0f}  {label}")
    hi = max(k, key=lambda x: x[2])
    lo_after = min((x for x in k if x[0] > hi[0]), key=lambda x: x[3], default=None)
    print(f"  максимум: ${hi[2]:,.0f} ({day(hi[0])})" +
          (f" · минимум после него: ${lo_after[3]:,.0f} ({day(lo_after[0])}, "
           f"{(lo_after[3] / hi[2] - 1) * 100:+.0f}%)" if lo_after else ""))
    print(f"  сейчас: ${k[-1][4]:,.0f} ({day(k[-1][0])})")
    moves = sorted(((c - o) / o * 100, day(ts), (h - l) / o * 100) for ts, o, h, l, c in k)
    print("  самые сильные дни вниз: " + " · ".join(f"{d} {m:+.1f}%" for m, d, _ in moves[:8]))
    print("  самые сильные дни вверх: " + " · ".join(f"{d} {m:+.1f}%" for m, d, _ in moves[-8:][::-1]))
    months = {}
    for ts, o, h, l, c in k:
        months.setdefault(day(ts)[:7], []).append((o, c))
    print("  по месяцам: " + " · ".join(f"{m[2:]} {(v[-1][1] / v[0][0] - 1) * 100:+.0f}%" for m, v in months.items()))


def fresh(days=30, workers=4):
    inst = scalp.fetch_instruments() or {}
    data, n = load_premium_price(days, workers)
    syms = [None] * n
    data = [(s, p, x) for s, p, x, _ in data]
    print(f"\nмонет: {len(data)} из {len(syms)} · {days} дн. · 5-мин premium index + цена · "
          f"вход на следующем баре после сигнала, выход ДО выплаты · издержки {COST}% вычтены")
    for thr in FRESH_THR:
        print(f"\n|premium| только что стал ≥ {thr*100:.1f}% (час до этого был спокоен):")
        for hold in FRESH_HOLDS:
            raw = []
            for s, prem, px in data:
                iv = inst.get(s, {}).get("funding_interval_min", 480) * 60_000
                raw += fresh_trades(prem, px, iv, thr, hold)
            if not raw:
                continue
            w = [r - COST for r, _ in raw]
            a = [-r - COST for r, _ in raw]
            print(f"  держим до {hold*5:>3} мин  С ТОЛПОЙ   {stats_line(w)}")
            print(f"  {'':15}  ПРОТИВ     {stats_line(a)}")
    # устойчивость лучшей клетки во времени
    raw = []
    for s, prem, px in data:
        iv = inst.get(s, {}).get("funding_interval_min", 480) * 60_000
        raw += fresh_trades(prem, px, iv, 0.002, 12)
    if raw:
        mid = sorted(t for _, t in raw)[len(raw) // 2]
        for name, sel in (("первая половина", lambda t: t < mid), ("вторая половина", lambda t: t >= mid)):
            sub = [r for r, t in raw if sel(t)]
            print(f"\n  ≥0.2%, 1ч — {name}: С ТОЛПОЙ {stats_line([r - COST for r in sub])} · "
                  f"ПРОТИВ {stats_line([-r - COST for r in sub])}")


def selftest():
    assert abs(tstat([1, 1, 1, 1]) - 0) < 1e-9  # zero variance → 0, no div-by-zero
    assert tstat([1, 2, 3, 4]) > 2
    assert session_of(0) == "Азия (Токио)" and session_of(14) == "Америка (Нью-Йорк)" and session_of(23) == "ночь США"
    T = 1_000_000_000_000
    k = [(T + m * 60_000, p, p, p, p) for m, p in ((-30, 100.0), (0, 102.0), (5, 101.0), (30, 99.0), (60, 98.0))]
    m = event_moves(k, T, -0.01)  # shorts pay 1% → bet up
    assert abs(m["pre"] - 2.0) < 1e-9 and m["post30"] < 0
    assert abs(m["trade"] - (1.0 + 1.0 - 0.11)) < 1e-9, m  # +1% price, +1% funding, −0.11% fees
    assert event_moves(k[:2], T, 0.01) is None  # missing candles → skipped, not guessed
    # longs paid (+) → go long at T+1m; price 100 → 103, dipped to 99 on the way
    k2 = [(T + m * 60_000, 100.0 + m * 0.5, 100.0 + m * 0.5, 99.0 if m == 2 else 100.0 + m * 0.5, 100.0 + m * 0.5)
          for m in range(0, 20)]
    net, worst = with_payers(k2, T, +0.003, 1, 4)   # entry open@T+1 = 100.5, exit open@T+5 = 102.5
    assert abs(net - ((102.5 - 100.5) / 100.5 * 100 - COST)) < 1e-9 and abs(worst - (99 - 100.5) / 100.5 * 100) < 1e-9
    net_s, _ = with_payers(k2, T, -0.003, 1, 4)    # shorts paid → short into the same rise loses
    assert net_s < 0
    # ticks: longs paid; last pre price 100, jump to 101 at T+0.1s, 101.5 by T+1s, exit 102 at T+300s
    Ts = 1_000_000.0
    tr = [(Ts - 5, 100.0), (Ts + 0.1, 101.0), (Ts + 1.0, 101.5), (Ts + 4, 101.5), (Ts + 11, 101.6),
          (Ts + 31, 101.8), (Ts + 300, 102.0)]
    m = tick_moves(tr, Ts, +0.002)
    assert abs(m["jump"][0] - 1.0) < 1e-9 and abs(m["jump"][1] - 1.5) < 1e-9
    assert abs(m["net"][0] - ((102 - 101) / 101 * 100 - COST)) < 1e-9
    assert m["net"][0] > m["net"][30]  # later entry = less left
    assert tick_moves(tr[:3], Ts, 0.002) is None  # no exit print → skipped
    # early entry at the last print ≤ T−5 (100), exit T+30 (101.8), pay 0.2% funding + costs
    assert abs(m["early"][30] - (1.8 - 0.2 - COST)) < 1e-9, m["early"]
    # fresh anomaly: calm hour, premium jumps to +0.3% at bar 12 → long entry at bar 13 open
    base = 8 * 3_600_000 * 100  # aligned to an 8h settlement
    prem = [(base + i * BAR, 0, 0, 0, 0.0003 if i != 12 else 0.003) for i in range(40)]
    assert fresh_signals(prem, 0.002) == [(12, 1)]
    assert fresh_signals([(base + i * BAR, 0, 0, 0, 0.003) for i in range(40)], 0.002) == []  # not fresh
    px = [(base + i * BAR, 100.0 + i, 0, 0, 0) for i in range(40)]
    tr = fresh_trades(prem, px, 8 * 3_600_000, 0.002, 6)
    assert tr == [((119.0 - 113.0) / 113.0 * 100, base + 13 * BAR)], tr   # 13 → 19
    # exit is capped before the next settlement: settlement 1h away, hold 4h
    near = 8 * 3_600_000 * 100 + 8 * 3_600_000 - 3_600_000 - 14 * BAR  # signal so entry lands 1h before T
    prem2 = [(near + i * BAR, 0, 0, 0, 0.0003 if i != 12 else 0.003) for i in range(80)]
    px2 = [(near + i * BAR, 100.0, 0, 0, 0) for i in range(80)]
    (r, t_in), = fresh_trades(prem2, px2, 8 * 3_600_000, 0.002, 48)
    assert t_in == near + 13 * BAR
    # spike path: long from 113 (bar 13), +1h = bar 25 open 125; settlement at bar 20 charged +0.1% (longs pay)
    fund = [(base + 20 * BAR, 0.001)]
    prem3 = [(base + i * BAR, 0, 0, 0, 0.0003 if i < 12 or i > 30 else 0.006) for i in range(200)]
    px3 = [(base + i * BAR, 100.0 + i, 0, 0, 0) for i in range(200)]
    (row,) = spike_path(prem3, px3, fund, 0.005)
    assert abs(row[1] - ((125 - 113) / 113 * 100 - 0.1 - COST)) < 1e-9, row
    assert abs(row["dur"] - 19 * BAR / 3_600_000) < 1e-9  # anomaly bar 12 → calm at bar 31
    print("study selftest ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "--selftest":
        selftest()
    elif cmd == "sessions":
        sessions()
    elif cmd == "funding":
        funding()
    elif cmd == "settlement":
        settlement()
    elif cmd == "ticks":
        settlement_ticks()
    elif cmd == "fresh":
        fresh()
    elif cmd == "spikes":
        spikes()
    elif cmd == "btc":
        btc_timeline()
    else:
        sys.exit(__doc__)

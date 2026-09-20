#!/usr/bin/env python3
"""Jev Crypto Scout — скрининг монет: количественные сигналы в коде,
качественные суждения о новостях (тональность/катализатор/достоверность) через Jev.

    python3 scout.py --top 50                  # только количественный скрининг, без Jev
    python3 scout.py --coins bitcoin,pepe,fartcoin  # свой список вместо топа по капе (id CoinGecko)
    python3 scout.py --top 30 --scalp           # + сканер фандинга/1м-осцилляторов на Bybit
    python3 scout.py --top 50 --news            # + новости через Jev (нужен ключ)
    python3 scout.py --selftest                 # без сети

НЕ торговый сигнал и не инвестиционный совет — инструмент для просмотра,
решение всегда за человеком. Ничего не покупает и не продаёт.
"""
import argparse, json, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from jev_client import Jev, pick_provider
import colors
import indicators

COINGECKO = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=1"          # без ключа
DEFILLAMA_URL = "https://api.llama.fi/v2/chains"              # без ключа
NEWS_FEEDS = ["https://cointelegraph.com/rss"]  # ponytail: один надёжный фид; больше — добавь в список

NEWS_QUESTIONS = {
    "sentiment": {"type": "choice", "instructions": "Тональность новости по отношению к упомянутой монете",
                  "criteria": {"bullish": "позитивная для цены: рост, принятие, листинг, партнёрство",
                               "bearish": "негативная: взлом, регуляторный запрет, иск, распродажа",
                               "neutral": "нейтральная или чисто информационная, без явного влияния на цену"}},
    "catalyst": {"type": "choice", "instructions": "Тип события, о котором эта новость",
                 "criteria": {"listing": "листинг на бирже или делистинг",
                              "hack_exploit": "взлом, эксплойт, кража средств",
                              "regulation": "регуляторное решение, суд, закон",
                              "partnership": "партнёрство, интеграция, институциональное принятие",
                              "hype_speculation": "спекуляция, мнение, прогноз без подтверждённых фактов",
                              "other": "не подходит ни под одну категорию выше"}},
    "confirmed": {"type": "score", "instructions": "Насколько новость подтверждена фактами, а не слухами",
                  "criteria": ["слух или анонимный источник", "частично подтверждено одной стороной",
                               "подтверждено официальным источником или документом"]},
}


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "jev-crypto-scout/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_markets(top_n=None, ids=None):
    """Топ N монет по капитализации — или свой список конкретных id (--coins),
    когда топ по капе не нужен и хочется следить за своим набором. Тот же
    эндпоинт, та же форма ответа — просто `ids=` вместо `order=`/`per_page=`."""
    base = f"{COINGECKO}/coins/markets?vs_currency=usd&price_change_percentage=24h,7d,30d&sparkline=false"
    url = f"{base}&ids={ids}" if ids else f"{base}&order=market_cap_desc&per_page={top_n}&page=1"
    return get_json(url)


def fetch_market_context():
    """Пара бесплатных индикаторов настроения рынка в целом — не для конкретной
    монеты, для фона. Обе отдают без ключа, обе тесты живьём проходят."""
    ctx = {}
    try:
        fng = get_json(FNG_URL, timeout=10)["data"][0]
        ctx["fear_greed"] = {"value": int(fng["value"]), "label": fng["value_classification"]}
    except Exception:
        ctx["fear_greed"] = None
    try:
        chains = get_json(DEFILLAMA_URL, timeout=10)
        ctx["defi_tvl_usd"] = sum(c.get("tvl") or 0 for c in chains)
    except Exception:
        ctx["defi_tvl_usd"] = None
    return ctx


def fetch_genesis_dates(coin_ids, workers=5):
    """Дата рождения монеты — по одному вызову на монету, поэтому только для короткого шортлиста."""
    def one(cid):
        try:
            d = get_json(f"{COINGECKO}/coins/{cid}"
                         f"?localization=false&tickers=false&market_data=false"
                         f"&community_data=false&developer_data=false", timeout=15)
            return cid, d.get("genesis_date")
        except Exception:
            return cid, None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, coin_ids))


def fetch_oscillators(coins, pace_seconds=1.3):
    """Осцилляторы TradingView-стиля (RSI/MACD/Bollinger/Stochastic), считаем сами
    по свечам CoinGecko OHLC (бесплатно, без ключа, есть high/low — для Stochastic
    этого мало у markets-эндпоинта, но хватает у /ohlc).

    ponytail: изначально пробовали Binance klines (public-apis подсказал) — тот же
    контент, но у него 451 Unavailable For Legal Reasons с части IP (геоблок биржи).
    CoinGecko работает без гео-ограничений, но free-тир жёстко лимитирует по частоте:
    пачка параллельных запросов быстро ловит 429 без восстановления в разумный бэкофф.
    Поэтому здесь — последовательно, с паузой между вызовами, не пул потоков. Для
    --top 30+ это минута-другая; для скрининга, не realtime-цены, это нормально.
    """
    out = {}
    for i, coin in enumerate(coins):
        if i > 0:
            time.sleep(pace_seconds)
        try:
            url = f"{COINGECKO}/coins/{coin['id']}/ohlc?vs_currency=usd&days=30"
            req = urllib.request.Request(url, headers={"User-Agent": "jev-crypto-scout/1"})
            kl = None
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        kl = json.load(r)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < 3:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise
            if not isinstance(kl, list) or len(kl) < 30:
                out[coin["symbol"].upper()] = None
                continue
            highs = [float(c[2]) for c in kl]
            lows = [float(c[3]) for c in kl]
            closes = [float(c[4]) for c in kl]
            rsi_v = indicators.rsi(closes)
            stoch_v = indicators.stochastic_k(highs, lows, closes)
            out[coin["symbol"].upper()] = {
                "rsi": rsi_v,
                "macd_hist": indicators.macd_histogram(closes),
                "boll_b": indicators.bollinger_percent_b(closes),
                "stoch": stoch_v,
                "signal": indicators.read_signal(rsi_v, stoch_v),
            }
        except Exception:
            out[coin["symbol"].upper()] = None  # редкая монета без OHLC-истории или сеть подвела
    return out


def coin_age_years(genesis_date):
    if not genesis_date:
        return None
    born = datetime.strptime(genesis_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - born).days / 365.25, 1)


def quant_signals(coin):
    """Всё это — арифметика, не суждение. Jev для такого не нужен и не годится
    (см. jaggedness §2 Math and Numbers в доках TypeSafe) — считаем сами."""
    mcap = coin.get("market_cap") or 0
    vol = coin.get("total_volume") or 0
    ath = coin.get("ath") or 0
    price = coin.get("current_price") or 0
    return {
        "pct_24h": coin.get("price_change_percentage_24h_in_currency"),
        "pct_7d": coin.get("price_change_percentage_7d_in_currency"),
        "pct_30d": coin.get("price_change_percentage_30d_in_currency"),
        "vol_to_mcap": round(vol / mcap, 4) if mcap else None,
        "dist_from_ath_pct": round((price / ath - 1) * 100, 1) if ath else None,
    }


def fetch_news():
    items = []
    for feed_url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "jev-crypto-scout/1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            print(f"  пропускаю фид {feed_url}: {e}", file=sys.stderr)
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            desc = re.sub("<[^>]+>", " ", item.findtext("description") or "")
            items.append({"title": title, "summary": " ".join(desc.split())[:400],
                          "link": item.findtext("link") or ""})
    return items


def match_news_to_coins(news, coins):
    """Дешёвый коарс-фильтр в коде: по названию/тикеру, до того как тратить запросы к Jev.
    Тот самый паттерн relevance-coarse-filter из экосистемы Jev — но без ИИ, просто подстрока."""
    out = []
    for n in news:
        text = f"{n['title']} {n['summary']}".lower()
        hits = [c for c in coins if c["name"].lower() in text
                or re.search(rf"\b{re.escape(c['symbol'])}\b", text)]
        if hits:
            out.append((n, hits))
    return out


def judge_news(jev, matched, workers=4):
    """Один запрос Jev на новость: тональность + тип катализатора + подтверждённость.
    Три вопроса за один round trip (speculative fan-out) вместо трёх отдельных запросов."""
    def one(pair):
        news, coins = pair
        r = jev.ask(f"{news['title']}. {news['summary']}", NEWS_QUESTIONS)
        a = r["answers"]
        return {"title": news["title"], "link": news["link"],
                "coins": [c["symbol"].upper() for c in coins],
                "sentiment": a["sentiment"]["choice"], "sent_conf": a["sentiment"]["confidence"],
                "catalyst": a["catalyst"]["choice"],
                "confirmed": a["confirmed"]["score"]}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, matched))


SENTIMENT_WEIGHT = {"bullish": 1, "neutral": 0, "bearish": -1}


def rank(coins, judged, osc_map=None):
    """Ранжирование — в коде. Веса и формула здесь, не в модели: их можно
    поменять без единого нового запроса к Jev (composite scoring, см. документ)."""
    news_by_symbol = {}
    for j in judged:
        for sym in j["coins"]:
            news_by_symbol.setdefault(sym, []).append(j)

    scored = []
    for c in coins:
        q = quant_signals(c)
        news = news_by_symbol.get(c["symbol"].upper(), [])
        news_score = sum(SENTIMENT_WEIGHT[n["sentiment"]] * n["confirmed"] for n in news)
        momentum = (q["pct_24h"] or 0) * 0.3 + (q["pct_7d"] or 0) * 0.7
        osc = (osc_map or {}).get(c["symbol"].upper())
        scored.append({**c, **q, "news_score": round(news_score, 2),
                       "news_count": len(news), "momentum": round(momentum, 2), "osc": osc})
    return sorted(scored, key=lambda x: (x["news_score"], x["momentum"]), reverse=True)


def print_report(ranked, judged, age_map=None, show_osc=False, context=None, scalp_results=None):
    if context:
        parts = []
        fg = context.get("fear_greed")
        if fg:
            fg_color = colors.red if fg["value"] >= 55 else (colors.green if fg["value"] <= 45 else colors.dim)
            parts.append("Fear&Greed " + fg_color(f"{fg['value']} {fg['label']}"))
        tvl = context.get("defi_tvl_usd")
        if tvl:
            parts.append("DeFi TVL " + colors.dim(f"${tvl / 1e9:,.0f}B"))
        if parts:
            print(colors.dim("🌡️  фон рынка: ") + "  ·  ".join(parts))

    osc_cols = f"{'RSI':>6}{'MACD':>8}{'%B':>6}{'Stoch':>7}{'сигнал':>11}" if show_osc else ""
    header = f"{'#':<3}{'монета':<8}{'капа':>16}{'24ч%':>8}{'7д%':>8}{'от ATH%':>10}{'vol/mcap':>10}{'возраст':>9}{'новости':>9}{osc_cols}"
    print("\n" + colors.bold(f"\U0001F4CA {header}"))
    for i, c in enumerate(ranked[:30], 1):
        age = age_map.get(c["id"]) if age_map else None
        age_s = f"{coin_age_years(age)}л" if age else "—"
        line = (f"  {i:<3}{c['symbol'].upper():<8}{c['market_cap']:>16,}"
                f"{colors.signed(c['pct_24h'], 8)}{colors.signed(c['pct_7d'], 8)}"
                f"{(c['dist_from_ath_pct'] or 0):>10.1f}{(c['vol_to_mcap'] or 0):>10.3f}"
                f"{age_s:>9}{c['news_score']:>9.1f}")
        if show_osc:
            o = c.get("osc")
            if o:
                line += (f"{(o['rsi'] if o['rsi'] is not None else 0):>6.0f}"
                         f"{colors.signed(o['macd_hist'], 8, 2)}"
                         f"{(o['boll_b'] if o['boll_b'] is not None else 0):>6.2f}"
                         f"{(o['stoch'] if o['stoch'] is not None else 0):>7.0f}"
                         f"{colors.pill(o['signal'], 11)}")
            else:
                line += f"{'—':>6}{'—':>8}{'—':>6}{'—':>7}{'':>11}"
        print(line)

    if scalp_results:
        print("\n" + colors.bold("⚡ scalp — фандинг + 1м-осцилляторы Bybit"))
        print(f"  {'символ':<10}{'funding_z':>10}{'RSI(1м)':>9}{'Stoch':>7}{'кандидат':>11}")
        for r in scalp_results:
            if "error" in r:
                print(f"  {r['symbol']:<10} {colors.dim('— ' + r['error'])}")
                continue
            f, o = r["funding"], r["osc"]
            tag_padded = f"{'ДА' if r['candidate'] else 'нет':>11}"
            tag = colors.red(tag_padded) if r["candidate"] else colors.dim(tag_padded)
            print(f"  {r['symbol']:<10}{colors.signed(f['z'], 10, 2)}"
                  f"{(o['rsi'] or 0):>9.0f}{(o['stoch'] or 0):>7.0f}{tag}")

    if judged:
        print("\n" + colors.bold(f"🧠 новости, разобранные Jev ({len(judged)}):"))
        for j in sorted(judged, key=lambda x: -x["confirmed"])[:15]:
            sent_color = colors.green if j["sentiment"] == "bullish" else (colors.red if j["sentiment"] == "bearish" else colors.dim)
            # confirmed — score 0..2 (слух/частично/официально), НЕ путать с sent_conf —
            # настоящей 0..1 уверенностью Choice-вопроса про тональность. Раньше оба
            # печатались под одной подписью "conf=" — легко спутать с калибровкой Jev.
            tag = sent_color(f"[{j['sentiment']}({j['sent_conf']:.2f})/{j['catalyst']}/подтв={j['confirmed']:.1f}]")
            print(f"  {','.join(j['coins']):<10} {tag:<45} {j['title'][:70]}")

    print(colors.dim("\n⚠️  Это скрининг, не сигнал на сделку. Решение — за тобой."))


def selftest():
    coins = [{"id": "x", "symbol": "x", "name": "Xcoin"}]
    news = [{"title": "Xcoin surges after listing", "summary": "Xcoin listed on major exchange", "link": "u"}]
    m = match_news_to_coins(news, coins)
    assert len(m) == 1 and m[0][1][0]["id"] == "x", m
    assert match_news_to_coins([{"title": "unrelated", "summary": "", "link": ""}], coins) == []
    q = quant_signals({"market_cap": 1000, "total_volume": 100, "ath": 10, "current_price": 5})
    assert q["vol_to_mcap"] == 0.1 and q["dist_from_ath_pct"] == -50.0, q
    assert coin_age_years("2009-01-03") > 15
    ranked = rank(coins, [{"coins": ["X"], "sentiment": "bullish", "confirmed": 1.0}])
    assert ranked[0]["news_score"] == 1.0, ranked
    print("selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=30, help="сколько монет по капитализации брать")
    p.add_argument("--coins", help="свой список вместо топа: id CoinGecko через запятую, напр. bitcoin,pepe,fartcoin")
    p.add_argument("--news", action="store_true", help="разбирать новости через Jev (нужен ключ)")
    p.add_argument("--age", action="store_true", help="подтягивать возраст монет (доп. запросы к CoinGecko)")
    p.add_argument("--ta", action="store_true", help="осцилляторы RSI/MACD/Bollinger/Stochastic по свечам CoinGecko OHLC")
    p.add_argument("--scalp", action="store_true", help="+ сканер фандинга/1м-осцилляторов Bybit на этих же монетах")
    p.add_argument("--fng", action="store_true", help="+ Fear&Greed Index и суммарный DeFi TVL (фон рынка)")
    p.add_argument("--save", help="сохранить сырые данные в JSON")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        selftest(); sys.exit()

    if args.coins:
        print(f"тяну свой список монет: {args.coins}…")
        coins = fetch_markets(ids=args.coins)
    else:
        print(f"тяну топ-{args.top} монет с CoinGecko…")
        coins = fetch_markets(top_n=args.top)

    context = fetch_market_context() if args.fng else None

    age_map = None
    if args.age:
        shortlist = [c["id"] for c in sorted(coins, key=lambda c: abs(c.get("price_change_percentage_24h_in_currency") or 0), reverse=True)[:10]]
        print(f"тяну дату рождения для {len(shortlist)} самых подвижных монет…")
        age_map = fetch_genesis_dates(shortlist)

    osc_map = None
    if args.ta:
        print(f"тяну свечи CoinGecko OHLC и считаю осцилляторы для {len(coins)} монет…")
        osc_map = fetch_oscillators(coins)

    scalp_results = None
    if args.scalp:
        import scalp as scalp_mod  # ленивый импорт: --scalp не всегда нужен, лишняя зависимость от Bybit по умолчанию не тянется
        symbols = [f"{c['symbol'].upper()}USDT" for c in coins if c["symbol"].lower() not in ("usdt", "usdc", "dai", "usds")]
        print(f"⚡ сканирую фандинг+1м-осцилляторы на Bybit для {len(symbols)} монет…")
        with ThreadPoolExecutor(max_workers=4) as pool:
            scalp_results = list(pool.map(scalp_mod.scan_symbol, symbols))

    judged = []
    if args.news:
        provider, key = pick_provider()
        jev = Jev(provider, key)
        print(f"тяну новости, провайдер Jev: {provider}…")
        news = fetch_news()
        matched = match_news_to_coins(news, coins)
        print(f"{len(news)} новостей, {len(matched)} касаются отслеживаемых монет — прогоняю через Jev…")
        judged = judge_news(jev, matched)

    ranked = rank(coins, judged, osc_map)
    print_report(ranked, judged, age_map, show_osc=args.ta, context=context, scalp_results=scalp_results)

    if args.save:
        json.dump({"coins": ranked, "news": judged}, open(args.save, "w"), ensure_ascii=False, indent=1)
        print(f"\nсырые данные -> {args.save}")

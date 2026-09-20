#!/usr/bin/env python3
"""Jev Crypto Scout — скрининг монет: количественные сигналы в коде,
качественные суждения о новостях (тональность/катализатор/достоверность) через Jev.

    python3 scout.py --top 50                  # только количественный скрининг, без Jev
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

COINGECKO = "https://api.coingecko.com/api/v3"
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


def fetch_markets(top_n):
    """Топ N монет по капитализации с ценой, объёмом, ATH и % изменения — всё уже посчитано CoinGecko."""
    url = (f"{COINGECKO}/coins/markets?vs_currency=usd&order=market_cap_desc"
           f"&per_page={top_n}&page=1&price_change_percentage=24h,7d,30d&sparkline=false")
    return get_json(url)


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


def rank(coins, judged):
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
        scored.append({**c, **q, "news_score": round(news_score, 2),
                       "news_count": len(news), "momentum": round(momentum, 2)})
    return sorted(scored, key=lambda x: (x["news_score"], x["momentum"]), reverse=True)


def print_report(ranked, judged, age_map=None):
    print(f"\n{'#':<3}{'монета':<8}{'капа':>16}{'24ч%':>8}{'7д%':>8}{'от ATH%':>10}"
          f"{'vol/mcap':>10}{'возраст':>9}{'новости':>9}")
    for i, c in enumerate(ranked[:30], 1):
        age = age_map.get(c["id"]) if age_map else None
        age_s = f"{coin_age_years(age)}л" if age else "—"
        print(f"{i:<3}{c['symbol'].upper():<8}{c['market_cap']:>16,}"
              f"{(c['pct_24h'] or 0):>8.1f}{(c['pct_7d'] or 0):>8.1f}"
              f"{(c['dist_from_ath_pct'] or 0):>10.1f}{(c['vol_to_mcap'] or 0):>10.3f}"
              f"{age_s:>9}{c['news_score']:>9.1f}")
    if judged:
        print(f"\nНовости, разобранные Jev ({len(judged)}):")
        for j in sorted(judged, key=lambda x: -x["confirmed"])[:15]:
            tag = f"[{j['sentiment']}/{j['catalyst']}/conf={j['confirmed']:.1f}]"
            print(f"  {','.join(j['coins']):<10} {tag:<45} {j['title'][:70]}")
    print("\nЭто скрининг, не сигнал на сделку. Решение — за тобой.")


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
    p.add_argument("--news", action="store_true", help="разбирать новости через Jev (нужен ключ)")
    p.add_argument("--age", action="store_true", help="подтягивать возраст монет (доп. запросы к CoinGecko)")
    p.add_argument("--save", help="сохранить сырые данные в JSON")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        selftest(); sys.exit()

    print(f"тяну топ-{args.top} монет с CoinGecko…")
    coins = fetch_markets(args.top)

    age_map = None
    if args.age:
        shortlist = [c["id"] for c in sorted(coins, key=lambda c: abs(c.get("price_change_percentage_24h_in_currency") or 0), reverse=True)[:10]]
        print(f"тяну дату рождения для {len(shortlist)} самых подвижных монет…")
        age_map = fetch_genesis_dates(shortlist)

    judged = []
    if args.news:
        provider, key = pick_provider()
        jev = Jev(provider, key)
        print(f"тяну новости, провайдер Jev: {provider}…")
        news = fetch_news()
        matched = match_news_to_coins(news, coins)
        print(f"{len(news)} новостей, {len(matched)} касаются отслеживаемых монет — прогоняю через Jev…")
        judged = judge_news(jev, matched)

    ranked = rank(coins, judged)
    print_report(ranked, judged, age_map)

    if args.save:
        json.dump({"coins": ranked, "news": judged}, open(args.save, "w"), ensure_ascii=False, indent=1)
        print(f"\nсырые данные -> {args.save}")

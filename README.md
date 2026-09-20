# Jev Crypto Scout — скрининг монет: цифры в коде, суждения через Jev

Скрининг топ-монет по капитализации: количественные сигналы (цена, объём, дистанция
от ATH, возраст монеты) считаются в коде на реальных данных CoinGecko. Новости
разбираются моделью [Jev](https://typesafe.ai) от TypeSafe (тональность / тип
катализатора / подтверждённость), а не читаются и не суммируются — Jev не
генерирует текст, только типизированные суждения.

**Не торговый бот и не инвестсовет.** Ничего не покупает, не продаёт, не даёт
сигналов на сделку. Печатает таблицу, которую смотрит человек.

---

## Почему так, а не иначе

Jev не умеет в математику — это прямо в его собственной документации
(«Math and Numbers», [jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)):
не считает надёжно и плохо работает с числовыми представлениями. Поэтому здесь
Jev **не считает индикаторы**. Все проценты, отношения объёма к капе, дистанция
от ATH — обычная арифметика в `quant_signals()`, без единого обращения к модели.

Jev берётся только там, где нужна семантика естественного языка: понять, что
новость про санкции — это `bearish`/`regulation`, а сплит ETF — `bullish`. Три
вопроса (тональность, катализатор, подтверждённость) уходят в **одном** запросе на
новость — [speculative fan-out](https://docs.typesafe.ai/patterns/fan-out), а не
три отдельных вызова.

Веса и формула ранжирования — в коде (`rank()`), не в модели. Поменять баланс
между новостным фоном и ценовым моментумом можно без единого нового обращения к
Jev — это паттерн [composite scoring](https://docs.typesafe.ai/patterns/composite-scoring).

## Устройство

```
CoinGecko /coins/markets  →  quant_signals()  (в коде: %24ч/7д/30д, vol/mcap, % от ATH)
CoinGecko /coins/{id}     →  coin_age_years()  (в коде: возраст по genesis_date, шортлист)
CoinGecko /coins/{id}/ohlc →  fetch_oscillators()  (в коде: RSI/MACD/Bollinger/Stochastic, indicators.py)
Cointelegraph RSS         →  match_news_to_coins()  (в коде: дешёвый фильтр по подстроке)
                           →  judge_news()  (Jev: sentiment + catalyst + confirmed, 1 запрос/новость)
                           →  rank()  (в коде: веса, сортировка)
                           →  print_report()
```

**Осцилляторы (`--ta`) — тоже чистый код, не Jev.** RSI, MACD-гистограмма, %B
Боллинджера, Stochastic %K — стандартные формулы в `indicators.py`, без
TA-Lib и вообще без внешних зависимостей. Свечи берём с CoinGecko OHLC:
пробовали сперва Binance (бесплатно, без ключа — подсказка из
[public-apis](https://github.com/public-apis/public-apis)), но у части IP
Binance отдаёт `451 Unavailable For Legal Reasons` (геоблок биржи).
CoinGecko OHLC работает без гео-ограничений, но free-тир жёстко лимитирует
частоту — поэтому `--ta` тянет свечи последовательно с паузой, не пачкой.

`jev_client.py` — трёхпровайдерный клиент Jev (нативный TypeSafe / Vercel AI
Gateway / Cloudflare Workers AI), с нормализацией расхождений между ними
(`noul` ↔ `boolean` ↔ `probability`, `input_tokens` ↔ `inputTokens`).

## Запуск

```bash
pip install --user 2>/dev/null || true   # зависимостей нет, только stdlib

python3 scout.py --top 30                       # только количественный скрининг, без Jev
python3 scout.py --top 30 --age                  # + возраст самых подвижных монет
python3 scout.py --top 30 --ta                   # + осцилляторы RSI/MACD/Bollinger/Stochastic
python3 scout.py --top 30 --news                 # + новости через Jev (нужен ключ)
python3 scout.py --selftest                      # без сети
python3 indicators.py                            # selftest осцилляторов отдельно
```

Ключ Jev — любой из трёх:

```bash
export AI_GATEWAY_API_KEY=vck_...                                   # Vercel
export TYPESAFE_API_KEY=...                                          # нативный
export CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=...            # Cloudflare
```

Без ключа `--news` завершается понятной ошибкой, а не падает. Для дымового
теста конвейера без ключа вообще — `JEV_PROVIDER=demo` (открытая модель в
Jev-образной обёртке, не сам Jev, но проверяет весь путь RSS→фильтр→разбор).

## Пример вывода

```
#  монета              капа    24ч%     7д%   от ATH%  vol/mcap  новости
1  BTC     1,629,576,499,053    -0.4     5.4     -35.7     0.015      3.8
2  ZEC       24,588,758,525    -2.7    32.9     -54.5     0.043      3.1

Новости, разобранные Jev:
  BTC   [bearish/regulation/conf=2.0]   US sanctions Iran's BitBank...
  ZEC   [bullish/other/conf=1.8]        Grayscale's Zcash ETF files for 3-for-1 split
  ADA   [bearish/hack_exploit/conf=1.7] Cardano's IOG warns of YouTube hijack
```

## Ограничения

- Один RSS-фид (Cointelegraph) — добавь свои в `NEWS_FEEDS`.
- Фильтр «новость ↔ монета» — подстрока по названию/тикеру в коде, не Jev.
  Дёшево и быстро, но пропустит непрямые упоминания.
- Возраст (`--age`) тянется по одной монете за раз с CoinGecko — только для
  шортлиста самых подвижных, не для всего топа (упрётесь в rate limit free-тира).
- `--ta` тоже упирается в free-тир CoinGecko: пара «—» в колонках осцилляторов —
  это честный прочерк после исчерпанных ретраев на 429, не баг и не «нет данных
  вообще». Для стабильности на большом `--top` — свой платный ключ CoinGecko
  или Binance-подобный источник без геоблока.
- Калибровка Jev на русскоязычном и финансовом английском контенте не
  верифицирована на большой выборке — см. более общий разбор технологии и
  замерялку калибровки в [typesafe-ai/skills](https://github.com/typesafe-ai/skills)
  и документации TypeSafe.

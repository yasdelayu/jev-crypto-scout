# Jev Crypto Scout

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#run-it)
[![Not financial advice](https://img.shields.io/badge/not-financial%20advice-red.svg)](ARCHITECTURE.md)

**[Русская версия](README.ru.md)**

Crypto screening: quantitative signals computed in code from real market
data (CoinGecko, Bybit); qualitative news judgment (sentiment / catalyst
type / confirmed-vs-rumor) via [Jev](https://typesafe.ai), TypeSafe's
System One model. See [ARCHITECTURE.md](ARCHITECTURE.md) for the honest
version of what this can and cannot do, including the scalping module, and
**[USAGE.md](USAGE.md) for a plain "I want X → run this → why" guide.**

**Not a trading bot and not investment advice.** It buys nothing, sells
nothing, and issues no trade signal — it prints a table a human reads.

---

## Why it's built this way

Jev cannot do math reliably — that's in TypeSafe's own docs
([jaggedness §2, "Math and Numbers"](https://docs.typesafe.ai/model-jaggedness/jev-1.13)).
So Jev never computes an indicator here. Every percentage, ratio, RSI,
MACD histogram, funding z-score is plain arithmetic in `indicators.py` /
`scout.py` / `scalp.py`, on real numbers from CoinGecko and Bybit — zero
model calls involved.

Jev is used only where natural-language judgment is the actual task:
is this news item bullish or bearish, what kind of event is it, how
confirmed is it. One request per news item covers three questions at
once ([speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)).
Ranking weights live in code (`rank()`), not in the model — you can
retune them without a single new Jev call
([composite scoring](https://docs.typesafe.ai/patterns/composite-scoring)).

## What's in the repo

| File | What |
|---|---|
| `scout.py` | Top-N coins by market cap: momentum, ATH distance, vol/mcap, coin age, oscillators, Jev-classified news |
| `scalp.py` | Short-horizon **signal scanner** (not an executor) — funding-rate extremity + 1-minute oscillators on Bybit, with Jev as a news-risk veto |
| `indicators.py` | RSI / MACD histogram / Bollinger %B / Stochastic %K — pure Python, no TA-Lib |
| `colors.py` | ANSI terminal colors, auto-off when not a TTY — no dependency |
| `http_client.py` | One shared HTTP GET with retry/backoff, used by scout.py and scalp.py |
| `listings.py` | Bybit listing/delisting tracker (`--listings`), no key |
| `telegram_notify.py` | Telegram digest delivery (`--telegram`) for scheduled runs, no dependency |
| `history.py` / `validate.py` | Opt-in SQLite run log (`--log`) and an honest forward-return check against it — not a finished backtest, see the file's own docstring |
| `jev_client.py` | One client across three Jev providers (TypeSafe / Vercel AI Gateway / Cloudflare Workers AI) |

## Run it

```bash
python3 scout.py --top 30                          # quant screening only, no Jev
python3 scout.py --coins bitcoin,pepe,fartcoin      # your own watchlist instead of top-by-cap
python3 scout.py --top 30 --age --ta                # + coin age + oscillators
python3 scout.py --top 30 --fng                     # + Fear&Greed Index, DeFi TVL (market mood)
python3 scout.py --top 30 --scalp                   # + Bybit funding/1m-oscillator scan, same coins
python3 scout.py --top 30 --news                    # + Jev news classification (needs a key)
python3 scout.py --top 30 --listings                 # + Bybit listings / delistings
python3 scout.py --top 30 --news --lang ru           # Russian-language feeds + questions to Jev in Russian

python3 scalp.py --symbols BTCUSDT,ETHUSDT,SOLUSDT          # funding + 1m oscillators, standalone
python3 scalp.py --symbols BTCUSDT --news                    # + Jev news veto

python3 scout.py --selftest && python3 scalp.py --selftest && python3 indicators.py
```

Colored terminal output (green/red by sign, amber for Jev tags) turns on
automatically on a real terminal and off automatically when piped to a
file — see `colors.py`, no `rich`/`colorama` dependency.

A Jev key, any of the three:

```bash
export AI_GATEWAY_API_KEY=vck_...                                   # Vercel
export TYPESAFE_API_KEY=...                                          # native
export CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=...            # Cloudflare
```

No key, no crash — `--news` fails with a clear message, not a traceback.
`JEV_PROVIDER=demo` runs the full pipeline against a free open-model
stand-in (not real Jev, but proves the wiring end to end with no key at all).

## What the scalp module actually is

Two independent signals, both plain arithmetic, both from Bybit's public
v5 API (no key needed for market data):

- **funding extremity** — how many standard deviations today's perp
  funding rate sits from its own recent history. A persistently high
  positive rate means longs are crowded and paying shorts.
- **1-minute oscillators** — the same RSI/Stochastic formulas as `--ta`,
  just on a much shorter timeframe.

A candidate needs **both** pointing the same direction at once — either
alone is noise. Jev enters exactly once per surviving candidate, as a
**veto**, not a predictor: is there a very recent, high-confidence,
bearish/regulatory news item about this symbol? If yes, the candidate is
suppressed — a funding/RSI extreme means nothing if the real driver is a
hack or a regulatory action no oscillator can see.

This is a screening aid on a minutes-scale polling loop, not a live
execution system. See [ARCHITECTURE.md](ARCHITECTURE.md) for what real
scalping infrastructure actually requires and why this repo deliberately
stops short of it.

## Data sources

- [CoinGecko](https://coingecko.com) — market data, OHLC, no key needed
- [Bybit v5](https://bybit-exchange.github.io/docs/v5/intro) — funding rate, 1m klines, no key needed for market data. Chosen over Binance, which returned `451 Unavailable For Legal Reasons` from part of our test infrastructure (exchange geo-blocking)
- [Cointelegraph RSS](https://cointelegraph.com/rss) — news source for the Jev classification step
- [CoinMarketCap](https://coinmarketcap.com/api/) — optional alternate market-data source. `listings/latest` works **keyless** (live-verified), `quotes/latest` does not (403 without a key). See `cmc.py`
- [Alternative.me Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/) — overall market mood, `--fng`, no key
- [DeFiLlama](https://defillama.com/docs/api) — total DeFi TVL across chains, `--fng`, no key

## Limitations

- One RSS feed by default — add more to `NEWS_FEEDS` in `scout.py`.
- News-to-coin matching is a word-boundary text filter in code, not Jev —
  cheap and fast, still misses indirect mentions (a headline naming a
  product without the coin's own name/ticker).
- `--age` and `--ta` hit CoinGecko's free-tier rate limit on a large
  `--top`; a dash in the table means a throttled call, not "no data".
- Bybit's own CloudFront front door has intermittently geo-blocked
  individual endpoints (`funding/history`, `instruments-info`) from part of
  our test infrastructure — distinct from Binance's outright `451` and from
  Bybit's own burst-traffic WAF block, and it comes and goes with routing.
  `scalp.py` degrades (shows the live rate without history-derived stats)
  rather than crashing when this happens; see `BybitGeoBlocked` in `scalp.py`.
- **No signal here has been backtested.** The candidate thresholds (funding
  z > 1.5, RSI 30/70) were chosen by convention, not validated against
  history. `history.py` + `validate.py` are the infrastructure for doing
  that honestly over time (`--log` on real runs) — there isn't yet enough
  logged data for a verdict, and free-tier API history is too thin/coarse
  to reconstruct a trustworthy backtest retroactively.
- Jev's calibration on financial English and on Russian has not been
  verified at scale here — see the broader technology writeup and
  calibration harness referenced in
  [typesafe-ai/skills](https://github.com/typesafe-ai/skills).

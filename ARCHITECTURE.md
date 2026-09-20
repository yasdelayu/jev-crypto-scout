# Architecture — what actually works here, and what real scalping needs

This document is the honest version. It says what this repo does, what it
doesn't, and what would have to change for any of it to touch real money.

## The one rule that shapes everything

**Jev does semantics. Code does arithmetic.** TypeSafe's own docs say
Jev is unreliable at counting, numeric comparison, and math in general
([jaggedness §2](https://docs.typesafe.ai/model-jaggedness/jev-1.13)). So
every number in this repo — price change %, vol/mcap ratio, RSI, MACD
histogram, Bollinger %B, Stochastic %K, funding z-score — is computed by
plain Python from real exchange data, with zero model calls. Jev is used
for exactly one kind of judgment: reading a news headline and deciding
its tone, its category, and how confirmed it is. That's it. Nowhere in
this repo does Jev predict a price, size a position, or decide to trade.

## Two pipelines, two honesty levels

### `scout.py` — screening, minutes-to-hours cadence

Pulls top-N coins by market cap, computes momentum/ATH-distance/vol-mcap
in code, optionally pulls coin age and daily-candle oscillators, and
optionally classifies matched news through Jev. This is exactly what it
looks like: a research dashboard you re-run when you want a look at the
market. Nothing here claims to be fast enough to trade on.

### `scalp.py` — signal candidates, not scalping

This is the part worth being blunt about, because "scalping" implies
speed this repo does not have.

**What real scalping infrastructure requires:**
- WebSocket market data (order book deltas, trade prints), not REST
  polling — a poll every few seconds misses the moves scalping targets
- Sub-10ms round trip to the exchange's matching engine, which means a
  server colocated near or inside the exchange's own infrastructure
- Execution logic that can cancel/replace an order faster than the
  market moves against it
- A backtest against tick-level historical data before any of it goes near
  real capital, and a paper-trading period after that

**What `scalp.py` actually is:** a polling loop over Bybit's public REST
API, once per invocation, computing two arithmetic signals (funding
extremity, 1-minute RSI/Stochastic) and combining them with an `AND` in
code. It has no execution path — it cannot place, cancel, or modify an
order, by construction, not by a disabled flag. The Jev call it makes
is a single classification of the most recent matching news item, used
as a veto. None of this is fast; none of it claims to be. Call it a
**short-horizon candidate screener**, because that's what it is.

## The funding-rate signal, in plain terms

A perpetual futures contract has no expiry, so exchanges use a funding
payment between longs and shorts to keep the perp price tracking spot.
When funding is persistently and unusually positive, it means the market
is crowded long and paying for it — a classic setup traders watch for
mean reversion or for delta-neutral carry (long spot, short perp, collect
the funding). `scalp.py`'s `funding_extremity()` computes exactly one
number: how many standard deviations the current rate sits from its own
recent history, on one exchange, informational only.

**What it is not:** a cross-exchange funding arbitrage engine. That needs
simultaneous data from multiple venues, a spread threshold net of fees
and slippage, and a two-leg execution path (spot leg + perp leg) with its
own risk controls — a materially bigger, separately-scoped system. If
that's the direction to go, it belongs in its own project with its own
execution discipline (trade-only API keys, no withdrawal rights, a
kill switch, a paper-trading period before real capital) — not bolted
onto a public demo repo whose entire point is to show Jev used honestly.

## Data sources and why each was picked

| Source | Used for | Why this one |
|---|---|---|
| CoinGecko `/coins/markets`, `/coins/{id}/ohlc` | market cap, momentum, daily oscillators | free, no key, no geo-blocking observed |
| Bybit v5 `/market/kline`, `/market/funding/history` | 1-minute candles, funding rate | free, no key for market data, **not** geo-blocked — Binance's equivalent endpoint returned `451 Unavailable For Legal Reasons` from part of our test infrastructure |
| Cointelegraph RSS | news text for the Jev classification step | free, stable, machine-readable |
| CoinMarketCap (optional) | alternate market-data source | needs your own free-tier key; see `cmc.py` — included as a documented fallback, not load-bearing |

Rate limits are real on every free tier above. `scout.py --ta`/`--age`
fetch sequentially with backoff rather than in a burst, and a dash in
the report means a throttled call, not missing data — see each module's
docstring for the exact numbers.

## What would have to change for "the real thing"

In order, cheapest to most involved:

1. **Backtest first.** Before trusting any signal here, replay it against
   months of historical funding-rate and candle data and measure it
   honestly — hit rate, average move after a flagged candidate, and how
   often the Jev veto would have saved you from a bad entry.
2. **WebSocket data.** Swap the REST polling in `scalp.py` for Bybit's
   WebSocket feeds — this alone turns "check every few minutes" into
   "react within seconds," still not scalping-grade, but a real step.
3. **Paper trading.** Run the signal live, on paper, for weeks, logging
   every candidate and what happened after — before any execution logic
   exists at all.
4. **Execution, only after the above.** A separate, carefully scoped
   system: trade-only API keys with no withdrawal rights, hard position
   and leverage limits in code (not configurable at runtime), a kill
   switch, and a human approving every trade until the track record
   justifies anything more automated. That system does not belong in
   this repo.

This repo's job is narrower and finishes at step 0: show that Jev,
used for exactly the judgment it's good at and nowhere else, is a
genuinely useful piece of a screening pipeline. Everything past that is
a different, higher-stakes project.

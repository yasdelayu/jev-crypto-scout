# Usage guide — what to run, and why

**[Русская инструкция](USAGE.ru.md)**

This is the practical guide. [README.md](README.md) explains the architecture;
this page just answers "I want X, what do I type, and what happens."

## The one command to start with

```bash
python3 scout.py --top 10
```

Pulls the top 10 coins by market cap and prints a table: price change over
24h/7d, how far below its all-time high each coin is, market cap, and how
much of the market cap trades in a day (`vol/mcap` — a low number can mean
a thin, easy-to-manipulate market). No Jev, no key needed, nothing else
required. This alone answers "what moved today."

## "I want to..." → command → why

| I want to... | Run this | Why this flag |
|---|---|---|
| See the market at a glance | `scout.py --top 20` | baseline: price, cap, momentum |
| Track *my own* coins, not the top-by-cap list | `scout.py --coins bitcoin,pepe,fartcoin` | ids are CoinGecko's own (same as in its URLs) — `--top` picks by rank, `--coins` picks by name |
| See if a coin is "overbought"/"oversold" right now | `...--ta` | adds RSI, MACD, Bollinger %B, Stochastic — the same numbers traders read off a chart, computed here from real candles |
| See if a coin is old money or a brand-new gamble | `...--age` | pulls each coin's birth date; `--top 10 --age` shows Bitcoin at 17 years next to something 3 months old |
| Know if the whole market is scared or greedy right now | `...--fng` | Fear & Greed Index + total DeFi TVL — market-wide mood, not per-coin |
| Catch a short-term overreaction (perp market crowding) | `...--scalp` | scans Bybit funding rate + 1-minute RSI on the same coins — see below, this is the one that needs its own explanation |
| Get an AI read on the news, not just the price | `...--news` | Jev reads each matched headline and tags it bullish/bearish, what kind of event, how confirmed — **needs a key**, see below |
| Ask Jev in Russian instead of English | `...--news --lang ru` | default is English because the news source (Cointelegraph) is English and Jev is documented to be better-calibrated in English; Russian is there for when you point it at a Russian-language feed |
| Save the raw numbers for later | `...--save out.json` | full data dump, not just what printed |
| Make sure nothing is broken, with no network | `scout.py --selftest` | runs in under a second, checks the math |

Combine freely: `scout.py --coins bitcoin,ethereum,pepe --ta --fng --scalp --news`
runs everything on your own watchlist in one shot.

## Reading the main table

```
📊 #  монета              капа    24ч%     7д%   от ATH%  vol/mcap  возраст  новости   RSI    MACD    %B  Stoch     сигнал
  1  BTC     1,628,167,540,058    -0.4    +4.8     -35.7     0.015        —      1.6    66  -10.41  0.67     77          —
```

- **капа** (market cap) — total value of every coin in circulation. Bigger
  usually means more established, less likely to move 50% overnight.
- **24ч% / 7д%** — price change over 1 day / 1 week.
- **от ATH%** (from all-time-high) — `-35.7` means the price is 35.7% below
  the highest it's ever been. A coin near 0 is at or near its peak; a coin
  near -90 has fallen a long way and may be either a bargain or a warning.
- **vol/mcap** — daily trading volume divided by market cap. Low (under
  ~0.02) can mean a thin market where a single large order moves the price
  a lot — worth knowing before you size a trade.
- **RSI** — 0 to 100. Under 30 is conventionally "oversold" (may bounce),
  over 70 is "overbought" (may pull back). It is not a prediction, just a
  reading of recent momentum.
- **MACD** — positive and growing means upward momentum is strengthening;
  negative and growing (more negative) means downward momentum is.
- **%B** — where the price sits inside its own recent volatility range.
  Near 0 = near the bottom of its recent range, near 1 = near the top.
- **Stoch** — similar idea to RSI, 0 to 100, same rough oversold/overbought
  reading, computed a different way (more sensitive to recent extremes).
- **сигнал** (signal) — code's own conclusion from RSI+Stochastic together:
  `▾ oversold` or `▴ overbought`. This is the one column meant to be read
  at a glance; the rest are inputs to it.

**None of these columns are Jev.** They are RSI, MACD, Bollinger, Stochastic —
standard formulas, computed in plain Python from real price history. Jev
touches none of this; it is arithmetic, not judgment.

## The `--scalp` funding line, explained

This was confusing before it got fixed, so here's the exact meaning now.

```
BTCUSDT        +1.95       55     31
    лонги платят шортам · 45×8ч подряд · сброс через 4ч11м
```

Perpetual futures (the contracts traded on Bybit) have no expiry date, so
the exchange makes traders on the crowded side pay traders on the other
side every settlement, to keep the contract's price tracking the real spot
price. That second, dim line answers exactly the three questions a bare
number can't:

- **who is paying whom** — "лонги платят шортам" (longs pay shorts) means
  more people are betting the price goes up than down, and they're paying
  for that privilege right now. The reverse ("шорты платят лонгам") means
  the crowd is leaning short.
- **how long this has been true** — `45×8ч` means 45 settlements in a row
  (Bybit settles funding every 8 hours here — some coins settle on a
  different schedule, this reads it from the exchange rather than assuming),
  roughly 15 days. A one-off blip means much less than a two-week streak.
- **when the next settlement happens** — `4ч11м`, a countdown, not a fixed
  clock time, so it's still accurate whenever you read it.

The `funding_z` number (`+1.95` above) is a separate, precise measurement:
how many standard deviations today's rate sits from its own recent average.
That's what the candidate logic actually uses — the readable line is for
you, the z-score is for the code.

**Why this matters at all:** a persistently one-sided, expensive funding
rate is a classic setup traders watch for mean reversion (the crowd is
over-committed and starts unwinding) — but it is a fact about current
positioning, not a prediction. `scalp.py` never tells you to act on it.

## What Jev actually does here — and what it needs

Everything above is arithmetic. Jev enters in exactly one place: reading
a news headline and answering three questions about it — is the tone
bullish/bearish/neutral, what kind of event is it (hack, regulation,
listing, partnership, speculation), and how confirmed is it (rumor vs.
official). That's it. It does not touch a single number on this page.

To turn that on:

```bash
export AI_GATEWAY_API_KEY=vck_...    # from vercel.com, AI Gateway → API Keys
python3 scout.py --top 20 --news
```

Put the key in a file, not in a chat message or a command you paste
somewhere public:

```bash
echo "AI_GATEWAY_API_KEY=vck_..." > ~/.jev.env
set -a; source ~/.jev.env; set +a
python3 scout.py --top 20 --news
```

Without a key, `--news` fails with a clear one-line message instead of a
crash — everything else still works.

## Two honest gaps you should know about

- **No candidates ≠ broken.** `--scalp` only flags a coin when funding
  *and* RSI are extreme at the same time. Most of the time, on most coins,
  that's simply not true — an empty candidates list is the correct answer
  on a calm day, not a bug.
- **A line you don't recognize on Bybit ("Symbol Invalid" or similar)** —
  as of the last fix this shouldn't happen anymore; the script checks
  Bybit's real instrument list before asking about a coin and tells you
  up front which ones aren't traded there as perpetuals, instead of
  guessing and failing per-symbol.

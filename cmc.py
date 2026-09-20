"""CoinMarketCap as an alternate/backup market-data source to CoinGecko.

Live-verified 2026-09-20: `listings/latest` works through the **keyless**
public API — no `X-CMC_PRO_API_KEY` header, no signup, same JSON shape as
the authenticated endpoint (just lower rate limits and a `credit_count`
in the response). `quotes/latest` (fetch by specific coin id) does NOT
work keyless — returns 403 "An API Key is required for this call." So
this module covers "give me the top-N market" the same way scout.py's
fetch_markets() does, as a drop-in alternate, not a full CMC client.

Set COINMARKETCAP_API_KEY to raise rate limits — same URL either way,
the key just moves from absent to an X-CMC_PRO_API_KEY header.
"""
import json, os, urllib.request

CMC_BASE = "https://pro-api.coinmarketcap.com/public-api/v1"


def fetch_listings(limit=30, convert="USD"):
    """Same intent as scout.fetch_markets(): top coins with price, market
    cap, and % change. Field names differ from CoinGecko's — see
    normalize_to_coingecko_shape() to feed this straight into scout.py."""
    url = f"{CMC_BASE}/cryptocurrency/listings/latest?limit={limit}&convert={convert}"
    headers = {"User-Agent": "jev-crypto-scout/1"}
    key = os.environ.get("COINMARKETCAP_API_KEY")
    if key:
        headers["X-CMC_PRO_API_KEY"] = key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["data"]


def normalize_to_coingecko_shape(cmc_rows, convert="USD"):
    """Reshapes CMC listings into the field names scout.py's quant_signals()
    and rank() already expect, so scout.py can run against either source
    without touching its own code — the adapter lives here, not there."""
    out = []
    for c in cmc_rows:
        q = c["quote"][convert]
        out.append({
            "id": c["slug"], "symbol": c["symbol"].lower(), "name": c["name"],
            "market_cap": q.get("market_cap"), "current_price": q.get("price"),
            "total_volume": q.get("volume_24h"),
            "price_change_percentage_24h_in_currency": q.get("percent_change_24h"),
            "price_change_percentage_7d_in_currency": q.get("percent_change_7d"),
            "price_change_percentage_30d_in_currency": q.get("percent_change_30d"),
            # CMC's keyless listings endpoint has no ATH field; scout.py's
            # dist_from_ath_pct will just come back None for this source.
            "ath": None,
        })
    return out


if __name__ == "__main__":
    rows = normalize_to_coingecko_shape(fetch_listings(5))
    for r in rows:
        print(f"{r['symbol'].upper():<6} {r['market_cap']:>18,.0f}  {r['price_change_percentage_24h_in_currency']:>6.2f}%")

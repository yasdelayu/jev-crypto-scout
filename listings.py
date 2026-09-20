"""Exchange listing / delisting tracker — a distinct, high-signal event type
that news feeds report late or not at all. Pulled straight from Bybit's
public announcements API (no key), live-verified 21.09.2026.

A new listing is a real, datable catalyst (fresh liquidity, attention); a
delisting is a real risk flag. This is code reading a structured feed, not
Jev — the classification (`[New Listings]` / `[Delistings]`) is Bybit's own,
so there's nothing to judge. Jev's role stays on the fuzzy stuff (news tone);
this is the crisp stuff.
"""
import time

from http_client import get_json

BYBIT_ANN = "https://api.bybit.com/v5/announcements/index?locale=en-US&limit=50"


def fetch_listings(include_tradfi=False):
    """Recent Bybit listings and delistings, newest first. Filters out
    TradFi perpetuals (tokenized stocks like AAPL/AMZU — `TradFi` in the
    title) by default, since a crypto screener cares about crypto listings;
    pass include_tradfi=True to keep them."""
    try:
        data = get_json(BYBIT_ANN, timeout=15)
    except Exception:
        return None  # announcements endpoint down/blocked — caller degrades, doesn't crash
    if not isinstance(data, dict) or data.get("retCode") != 0:
        return None

    out = []
    for a in data["result"]["list"]:
        title = a.get("title", "")
        low = title.lower()
        # Content-first: Bybit's own category is noisy (a promo can be filed
        # under "New Listings"), so require the word in the title itself.
        if "delist" in low:
            kind = "delisting"
        elif "new listing" in low or "will list" in low:
            kind = "listing"
        else:
            continue
        if not include_tradfi and "tradfi" in low:
            continue
        out.append({"kind": kind, "title": title.strip(),
                    "url": a.get("url", ""),
                    "ts": a.get("dateTimestamp") or a.get("publishTime") or 0})
    return out


def format_listings(listings, limit=10):
    """Compact lines for the terminal / Telegram digest."""
    if not listings:
        return []
    lines = []
    for l in listings[:limit]:
        mark = "📈" if l["kind"] == "listing" else "📉"
        lines.append(f"{mark} {l['title'][:70]}")
    return lines


def selftest():
    # filtering logic in isolation, offline — no network
    sample = [
        {"kind": "listing", "title": "New listing: FOOUSDT Perpetual Contract"},
        {"kind": "delisting", "title": "Bybit to Delist BARUSDT"},
    ]
    lines = format_listings(sample)
    assert lines[0].startswith("📈") and "FOO" in lines[0], lines
    assert lines[1].startswith("📉") and "BAR" in lines[1], lines
    assert format_listings(None) == []
    assert format_listings([]) == []
    print("listings selftest ok")


if __name__ == "__main__":
    selftest()

"""Telegram delivery for scheduled runs — turns a scout/scalp result into a
short digest and sends it to a chat via the Bot API. No dependency: the Bot
API is a plain HTTPS POST.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment. Used by
scout.py --telegram; on a server this is what a cron/systemd timer calls so
the table reaches your phone instead of a log nobody reads.
"""
import html, json, os, urllib.parse, urllib.request


def _send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в окружении")
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.load(r)
    if not out.get("ok"):
        raise RuntimeError(f"Telegram API: {out}")
    return out


def _arrow(v):
    if v is None:
        return "·"
    return f"🟢+{v:.1f}%" if v >= 0 else f"🔴{v:.1f}%"


def format_digest(ranked, judged=None, scalp_results=None, context=None, listings_data=None, top_n=8):
    """Short HTML digest for a phone screen — not the full terminal table.
    Leads with market mood, then the top movers, then any oscillator signals,
    then Jev's bearish/bullish news flags. Deliberately compact."""
    lines = ["<b>📊 Jev Crypto Scout</b>"]

    if context and context.get("fear_greed"):
        fg = context["fear_greed"]
        tvl = context.get("defi_tvl_usd")
        mood = f"🌡 Fear&amp;Greed <b>{fg['value']} {fg['label']}</b>"
        if tvl:
            mood += f" · DeFi TVL ${tvl / 1e9:,.0f}B"
        lines.append(mood)

    lines.append("")
    for c in ranked[:top_n]:
        osc = c.get("osc") or {}
        sig = osc.get("signal")
        badge = " ⚠️" + sig if sig in ("oversold", "overbought") else ""
        news = f" 📰{c['news_score']:+.1f}" if c.get("news_score") else ""
        sym = html.escape(c["symbol"].upper())
        lines.append(f"<code>{sym:<6}</code> 7д {_arrow(c.get('pct_7d'))}{badge}{news}")

    if scalp_results:
        cands = [r for r in scalp_results if r.get("candidate")]
        if cands:
            lines.append("\n<b>⚡ scalp-кандидаты:</b>")
            for r in cands:
                lines.append(f"<code>{html.escape(r['symbol'])}</code> funding z={r['funding'].get('z')}")
        else:
            lines.append("\n<i>⚡ scalp: кандидатов нет (спокойно)</i>")

    if judged:
        flagged = [j for j in judged if j["sentiment"] in ("bearish", "bullish")
                   and j.get("sent_conf", 0) > 0.7]
        if flagged:
            lines.append("\n<b>🧠 новости (Jev):</b>")
            for j in sorted(flagged, key=lambda x: -x["confirmed"])[:5]:
                emoji = "🔴" if j["sentiment"] == "bearish" else "🟢"
                coins = html.escape(",".join(j["coins"]))
                title = html.escape(j["title"][:60])
                lines.append(f"{emoji} <code>{coins}</code> {html.escape(j['catalyst'])}: {title}")

    if listings_data:
        lines.append("\n<b>📋 листинги/делистинги (Bybit):</b>")
        for l in listings_data[:6]:
            mark = "📈" if l["kind"] == "listing" else "📉"
            lines.append(f"{mark} {html.escape(l['title'][:60])}")

    lines.append("\n<i>Скрининг, не сигнал на сделку.</i>")
    return "\n".join(lines)


def notify(ranked, judged=None, scalp_results=None, context=None, listings_data=None):
    return _send(format_digest(ranked, judged, scalp_results, context, listings_data))


def selftest():
    # format_digest must not crash on any subset of inputs, and must escape safely
    d = format_digest(
        [{"symbol": "btc", "pct_7d": 5.0, "osc": {"signal": "overbought"}, "news_score": 1.2},
         {"symbol": "eth", "pct_7d": -3.0, "osc": {}, "news_score": 0}],
        judged=[{"coins": ["BTC"], "sentiment": "bearish", "sent_conf": 0.9,
                 "confirmed": 1.8, "catalyst": "regulation", "title": "US sanctions <thing> & more"}],
        scalp_results=[{"symbol": "ETHUSDT", "candidate": True, "funding": {"z": 2.1}}],
        context={"fear_greed": {"value": 71, "label": "Greed"}, "defi_tvl_usd": 93e9})
    assert "Jev Crypto Scout" in d
    assert "overbought" in d and "BTC" in d
    assert "&amp;" in d  # the & in Fear&Greed was escaped
    assert "US sanctions &lt;thing&gt; &amp; more" in d  # the title's < & > were escaped, not raw
    assert "<thing>" not in d  # raw angle brackets would break Telegram HTML parse
    # empty case
    d2 = format_digest([], None, None, None)
    assert "Jev Crypto Scout" in d2
    print("telegram_notify selftest ok")


if __name__ == "__main__":
    selftest()

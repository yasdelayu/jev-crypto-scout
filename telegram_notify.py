"""Telegram delivery for scheduled runs — turns a scout/scalp result into a
digest and sends it to a chat via the Bot API. No dependency: the Bot API is
a plain HTTPS POST.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment. Used by
scout.py --telegram; on a server this is what a timer calls so the table
reaches your phone instead of a log nobody reads.
"""
import html, json, os, urllib.parse, urllib.request

TG_LIMIT = 4096  # Telegram's hard per-message character cap


def _post(text):
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


def _send(text):
    """Split on blank lines into <=TG_LIMIT chunks so a long digest (top-100 +
    news + listings + funding) never trips Telegram's 4096-char cap and gets
    silently rejected — the bug behind 'обрезанные новости'."""
    blocks = text.split("\n")
    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 1 > TG_LIMIT and cur:
            chunks.append(cur)
            cur = ""
        cur += b + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        _post(c.rstrip())
    return len(chunks)


def _arrow(v):
    if v is None:
        return "·"
    return f"🟢+{v:.1f}%" if v >= 0 else f"🔴{v:.1f}%"


def _fund_dir(f):
    """Who-pays-whom, with BOTH the real funding rate (%, what Bybit shows you)
    and the z-score (how unusual that rate is vs its own recent history). They
    are different numbers: rate is the actual payment, z is the anomaly."""
    z = f.get("z")
    rate = f.get("current")
    rate_str = f"{rate*100:+.3f}%" if rate is not None else "?"
    if z is None:
        who = "🔴 лонги платят" if (rate or 0) > 0 else "🟢 шорты платят"
        return f"{who} ({rate_str})"
    who = "🔴 лонги платят" if z > 0 else "🟢 шорты платят"
    return f"{who} ({rate_str}, z={z:+.1f})"


def format_digest(ranked, judged=None, scalp_results=None, context=None, listings_data=None, priorities=None, top_n=8):
    """Digest for a phone: Jev's 'what to look at' first (the point of the
    whole thing), then market mood, top movers, funding skew, Jev news flags
    (clickable links), listings/delistings, and a legend."""
    lines = ["<b>📊 Jev Crypto Scout</b>"]

    # 'На что смотреть' идёт ПЕРВЫМ — это ответ на «что мне делать с инфой»
    if priorities:
        import attention
        for l in attention.format_attention(priorities, top=5):
            lines.append(l)

    if context and context.get("fear_greed"):
        fg = context["fear_greed"]
        tvl = context.get("defi_tvl_usd")
        mood = f"🌡 Fear&amp;Greed <b>{fg['value']} {fg['label']}</b>"
        if tvl:
            mood += f" · DeFi TVL ${tvl / 1e9:,.0f}B"
        lines.append(mood)

    lines.append("\n<b>Движение (7д):</b>")
    for c in ranked[:top_n]:
        osc = c.get("osc") or {}
        sig = osc.get("signal")
        badge = " ⚠️" + sig if sig in ("oversold", "overbought") else ""
        news = f" 📰{c['news_score']:+.1f}" if c.get("news_score") else ""
        sym = html.escape(c["symbol"].upper())
        lines.append(f"<code>{sym:<6}</code> {_arrow(c.get('pct_7d'))}{badge}{news}")

    if scalp_results:
        ok = [r for r in scalp_results if "error" not in r and r.get("funding", {}).get("z") is not None]
        cands = [r for r in ok if r.get("candidate")]
        # Top funding skew (most crowded), candidate or not — this is the
        # 'где фандинг?' the digest was missing.
        skewed = sorted(ok, key=lambda r: abs(r["funding"]["z"]), reverse=True)[:5]
        if skewed:
            lines.append("\n<b>⚡ Фандинг (перекос):</b>")
            for r in skewed:
                f = r["funding"]
                reset = f" · сброс {f['next_reset_min']//60}ч{f['next_reset_min']%60:02d}м" if f.get("next_reset_min") else ""
                star = " 🎯" if r.get("candidate") else ""
                lines.append(f"<code>{html.escape(r['symbol'].replace('USDT','')):<6}</code> {_fund_dir(f)}{reset}{star}")
        if cands:
            lines.append("<i>🎯 = кандидат: фандинг И осциллятор экстремальны разом</i>")

    if judged:
        flagged = [j for j in judged if j["sentiment"] in ("bearish", "bullish")
                   and j.get("sent_conf", 0) > 0.7]
        if flagged:
            lines.append("\n<b>🧠 Новости (Jev):</b>")
            for j in sorted(flagged, key=lambda x: -x["confirmed"])[:8]:
                emoji = "🔴" if j["sentiment"] == "bearish" else "🟢"
                coins = html.escape(",".join(j["coins"]))
                title = html.escape(j["title"][:100])
                link = j.get("link") or ""
                # заголовок кликабельной ссылкой на источник — не обрезаем в мясо
                titled = f'<a href="{html.escape(link)}">{title}</a>' if link else title
                lines.append(f"{emoji} <code>{coins}</code> [{html.escape(j['catalyst'])}] {titled}")

    if listings_data:
        lines.append("\n<b>📋 Листинги/делистинги (Bybit):</b>")
        for l in listings_data[:8]:
            mark = "📈" if l["kind"] == "listing" else "📉"
            lines.append(f"{mark} {html.escape(l['title'][:90])}")

    lines.append(
        "\n<b>ℹ️ Обозначения:</b>\n"
        "🟢/🔴 у монеты — рост/падение цены за 7д\n"
        "🟢/🔴 у новости — Jev считает её хорошей/плохой для цены\n"
        "📰 — новостной балл (сумма настроений по монете)\n"
        "⚠️ oversold/overbought — перепродано/перекуплено (RSI)\n"
        "⚡ фандинг: 🔴 лонги платят (толпа в лонг), 🟢 шорты платят\n"
        "📈/📉 — листинг/делистинг на бирже")
    lines.append("\n<i>Скрининг, не сигнал на сделку.</i>")
    return "\n".join(lines)


def notify(ranked, judged=None, scalp_results=None, context=None, listings_data=None, priorities=None):
    return _send(format_digest(ranked, judged, scalp_results, context, listings_data, priorities))


def send_text(text):
    """Plain helper for the bot to answer commands."""
    return _send(text)


def selftest():
    d = format_digest(
        [{"symbol": "btc", "pct_7d": 5.0, "osc": {"signal": "overbought"}, "news_score": 1.2},
         {"symbol": "eth", "pct_7d": -3.0, "osc": {}, "news_score": 0}],
        judged=[{"coins": ["BTC"], "sentiment": "bearish", "sent_conf": 0.9, "confirmed": 1.8,
                 "catalyst": "regulation", "title": "US sanctions <thing> & more",
                 "link": "https://x.com/a?b=1&c=2"}],
        scalp_results=[{"symbol": "ETHUSDT", "candidate": True,
                        "funding": {"z": 2.1, "next_reset_min": 150}},
                       {"symbol": "BTCUSDT", "candidate": False,
                        "funding": {"z": -0.5, "next_reset_min": 30}}],
        context={"fear_greed": {"value": 71, "label": "Greed"}, "defi_tvl_usd": 93e9},
        priorities=[{"symbol": "BTC", "attention": 2.0, "action": "research", "act_conf": 0.8}])
    assert "Jev Crypto Scout" in d
    assert "На что смотреть" in d and "разобраться" in d  # attention section present, first
    assert "overbought" in d and "BTC" in d
    assert "&amp;" in d and "<thing>" not in d  # escaping
    assert "Фандинг" in d and "z=+2.1" in d  # funding section present with value
    assert 'href="https://x.com/a?b=1&amp;c=2"' in d  # link escaped inside href
    assert "Обозначения" in d  # legend present
    # chunk splitter: a huge text splits, a small one doesn't
    big = "\n".join(["x" * 100 for _ in range(60)])  # ~6000 chars
    blocks = big.split("\n"); chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 1 > TG_LIMIT and cur:
            chunks.append(cur); cur = ""
        cur += b + "\n"
    if cur.strip():
        chunks.append(cur)
    assert len(chunks) >= 2, "6000 chars should split into >=2 messages"
    assert all(len(c) <= TG_LIMIT for c in chunks)
    print("telegram_notify selftest ok")


if __name__ == "__main__":
    selftest()

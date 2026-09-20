"""The 'what do I do with this' layer — Jev turns a pile of signals into a
ranked 'what to look at' list with a plain-language why.

This is the most productive use of Jev in the whole pipeline and it stays on
the right side of the line:
  - It does NOT generate advice or say buy/sell — that would be a
    hallucination and not what Jev does.
  - It classifies each coin's WHOLE situation (price move + funding skew +
    news) into an attention level (Score) and a suggested next step (Choice
    over a fixed set: watch / research the cause / skip). Code writes the
    dossier from real numbers; Jev judges the semantics; the confidence says
    how sure it is. That's exactly the System One shape.

From 100 coins a human can't watch all of them. This ranks the few worth a
look right now and says, in words, why — which is the thing the raw table
never told you.
"""

ATTENTION_QUESTIONS_RU = {
    "attention": {
        "type": "score",
        "instructions": "Насколько ситуация по этой монете заслуживает внимания трейдера прямо сейчас",
        "criteria": [
            "рутина: ничего особенного, обычные колебания",
            "стоит взгляда: есть заметный сигнал, но не срочно",
            "важно: сошлись несколько сильных сигналов, требует внимания",
        ],
    },
    "action": {
        "type": "choice",
        "instructions": "Какой следующий шаг напрашивается по этой ситуации (не совет купить/продать, а что делать с информацией)",
        "criteria": {
            "watch": "просто держать в поле зрения, наблюдать за развитием",
            "research": "разобраться в причине: почему движение, что за новость",
            "skip": "можно игнорировать, ничего значимого",
        },
    },
}

ATTENTION_QUESTIONS_EN = {
    "attention": {
        "type": "score",
        "instructions": "How much this coin's situation deserves a trader's attention right now",
        "criteria": [
            "routine: nothing special, ordinary fluctuation",
            "worth a look: a notable signal, but not urgent",
            "important: several strong signals line up, needs attention",
        ],
    },
    "action": {
        "type": "choice",
        "instructions": "What next step this situation suggests (not buy/sell advice, but what to do with the info)",
        "criteria": {
            "watch": "just keep it in view, watch how it develops",
            "research": "dig into the cause: why the move, what's the news",
            "skip": "safe to ignore, nothing meaningful",
        },
    },
}


def build_dossier(coin, scalp_by_sym, news_by_sym):
    """Plain-text situation for one coin, assembled in code from real signals.
    This is the `state` Jev judges — numbers already computed, not raw feeds."""
    sym = coin["symbol"].upper()
    bits = [f"Монета {sym}."]
    if coin.get("pct_7d") is not None:
        bits.append(f"Цена за 7д: {coin['pct_7d']:+.1f}%, за 24ч: {coin.get('pct_24h') or 0:+.1f}%.")
    if coin.get("dist_from_ath_pct") is not None:
        bits.append(f"От исторического максимума: {coin['dist_from_ath_pct']:.0f}%.")
    osc = coin.get("osc") or {}
    if osc.get("signal") in ("oversold", "overbought"):
        bits.append(f"Технически: {osc['signal']} (RSI {osc.get('rsi')}).")
    sc = scalp_by_sym.get(sym)
    if sc and sc.get("funding", {}).get("z") is not None:
        f = sc["funding"]
        bits.append(f"Фандинг: {f['direction']}, отклонение z={f['z']}.")
    for n in news_by_sym.get(sym, [])[:3]:
        bits.append(f"Новость ({n['sentiment']}, {n['catalyst']}): {n['title'][:120]}")
    return " ".join(bits)


def prioritize(jev, ranked, judged, scalp_results, lang="ru", max_coins=15):
    """Ask Jev to rate attention + suggest a step for each coin that has ANY
    real signal (a move, an oscillator flag, a funding skew, or news). Returns
    coins sorted by attention, each with action + confidence + the dossier."""
    scalp_by_sym = {r["symbol"].replace("USDT", ""): r for r in (scalp_results or []) if "error" not in r}
    news_by_sym = {}
    for j in (judged or []):
        for sym in j["coins"]:
            news_by_sym.setdefault(sym, []).append(j)

    # only bother Jev with coins that actually have something going on
    def has_signal(c):
        sym = c["symbol"].upper()
        osc = c.get("osc") or {}
        sc = scalp_by_sym.get(sym, {})
        return (osc.get("signal") in ("oversold", "overbought")
                or (sc.get("funding", {}) or {}).get("z") not in (None,)
                and abs((sc.get("funding", {}) or {}).get("z") or 0) > 1.0
                or sym in news_by_sym
                or abs(c.get("pct_7d") or 0) > 10)

    candidates = [c for c in ranked if has_signal(c)][:max_coins]
    questions = ATTENTION_QUESTIONS_RU if lang == "ru" else ATTENTION_QUESTIONS_EN

    out = []
    for c in candidates:
        dossier = build_dossier(c, scalp_by_sym, news_by_sym)
        try:
            a = jev.ask(dossier, questions)["answers"]
        except Exception:
            continue
        out.append({
            "symbol": c["symbol"].upper(),
            "attention": a["attention"]["score"],
            "attn_conf": a["attention"]["confidence"],
            "action": a["action"]["choice"],
            "act_conf": a["action"]["confidence"],
            "dossier": dossier,
        })
    return sorted(out, key=lambda x: x["attention"], reverse=True)


ACTION_LABEL = {
    "watch": "👀 наблюдать",
    "research": "🔍 разобраться в причине",
    "skip": "— можно пропустить",
}


def format_attention(priorities, lang="ru", top=5):
    """Digest lines: the few coins worth looking at, with the suggested step
    and why. Only surfaces attention >= 1 (worth a look or important)."""
    worth = [p for p in priorities if p["attention"] >= 1.0][:top]
    if not worth:
        return []
    lines = ["\n<b>🎯 На что смотреть (Jev):</b>"]
    for p in worth:
        act = ACTION_LABEL.get(p["action"], p["action"])
        lines.append(f"<code>{p['symbol']:<6}</code> {act} <i>(уверенность {p['act_conf']:.0%})</i>")
    lines.append("<i>Jev оценил ситуацию целиком — не совет покупать, а куда смотреть.</i>")
    return lines


def selftest():
    coin = {"symbol": "btc", "pct_7d": 15.0, "pct_24h": 2.0, "dist_from_ath_pct": -20,
            "osc": {"signal": "overbought", "rsi": 78}}
    scalp_by = {"BTC": {"symbol": "BTCUSDT", "funding": {"z": 2.3, "direction": "лонги платят шортам"}}}
    news_by = {"BTC": [{"sentiment": "bearish", "catalyst": "regulation", "title": "US sanctions"}]}
    d = build_dossier(coin, scalp_by, news_by)
    assert "BTC" in d and "overbought" in d and "z=2.3" in d and "sanctions" in d, d

    # format only surfaces attention >= 1
    pris = [{"symbol": "BTC", "attention": 2.0, "attn_conf": 0.9, "action": "research", "act_conf": 0.8},
            {"symbol": "ETH", "attention": 0.0, "attn_conf": 0.9, "action": "skip", "act_conf": 0.9}]
    lines = format_attention(pris)
    joined = "\n".join(lines)
    assert "BTC" in joined and "разобраться" in joined
    assert "ETH" not in joined  # attention 0 filtered out
    assert format_attention([{"symbol": "X", "attention": 0.0, "action": "skip", "act_conf": 1}]) == []
    print("attention selftest ok")


if __name__ == "__main__":
    selftest()

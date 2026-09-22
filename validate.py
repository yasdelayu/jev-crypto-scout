"""Self-check: reads history.db and asks whether the signals actually predict
anything — the honest answer to 'что с этим делать'. You act on a signal only
to the degree it has beaten random on YOUR accumulated data, not because a
tweet said 81ms and Kelly sizing.

The one number that matters is EDGE: a signal's average forward move MINUS the
baseline (the average forward move of ALL coins over the same horizon). If a
signal's forward return isn't clearly better than just picking any coin, it
has no edge and you shouldn't weight it — no matter how confident the model
sounded. This is the opposite of Kelly-betting on a model's raw confidence.

NOT a finished backtest. Free-tier point-in-time data is thin; a real verdict
needs weeks of logged runs. Until then this prints n= and withholds a verdict
below --min-samples. The honest path is: keep logging (`--log`), check back.

    python3 validate.py                    # human-readable report
    python3 validate.py --json             # machine summary (bot /stats uses this)
    python3 validate.py --min-samples 30
"""
import argparse
import history


def _forward_price(conn, symbol, after_ts, horizon_s):
    row = conn.execute(
        "SELECT price FROM scout_runs WHERE symbol=? AND ts>=? AND price IS NOT NULL ORDER BY ts ASC LIMIT 1",
        (symbol, after_ts + horizon_s)).fetchone()
    return row[0] if row else None


def _price_at(conn, symbol, ts):
    row = conn.execute(
        "SELECT price FROM scout_runs WHERE symbol=? AND ts=? AND price IS NOT NULL LIMIT 1",
        (symbol, ts)).fetchone()
    return row[0] if row else None


def baseline_move(conn, horizon_h):
    """Average forward % move of every logged (coin, time) point — what you'd
    get picking coins at random. The bar every signal must clear."""
    rows = conn.execute("SELECT symbol, ts, price FROM scout_runs WHERE price IS NOT NULL").fetchall()
    moves = []
    for symbol, ts, price in rows:
        fwd = _forward_price(conn, symbol, ts, horizon_h * 3600)
        if fwd:
            moves.append((fwd - price) / price * 100)
    return moves


def signal_move(conn, horizon_h, where):
    """Forward moves for scout rows matching `where` (e.g. signal='oversold')."""
    rows = conn.execute(f"SELECT symbol, ts, price FROM scout_runs WHERE price IS NOT NULL AND {where}").fetchall()
    moves = []
    for symbol, ts, price in rows:
        fwd = _forward_price(conn, symbol, ts, horizon_h * 3600)
        if fwd:
            moves.append((fwd - price) / price * 100)
    return moves


def attention_move(conn, horizon_h, action):
    """Forward moves after Jev flagged a coin with a given action, priced from
    the scout row at the same timestamp (attention_runs has no price of its own)."""
    rows = conn.execute("SELECT symbol, ts FROM attention_runs WHERE action=?", (action,)).fetchall()
    moves = []
    for symbol, ts in rows:
        price = _price_at(conn, symbol, ts)
        if not price:
            continue
        fwd = _forward_price(conn, symbol, ts, horizon_h * 3600)
        if fwd:
            moves.append((fwd - price) / price * 100)
    return moves


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def compute(conn, horizon_h=24):
    base = baseline_move(conn, horizon_h)
    base_mean = _mean(base)
    checks = []
    specs = [
        ("oversold (RSI)", "signal='oversold'", signal_move(conn, horizon_h, "signal='oversold'")),
        ("overbought (RSI)", "signal='overbought'", signal_move(conn, horizon_h, "signal='overbought'")),
        ("Jev: research", None, attention_move(conn, horizon_h, "research")),
        ("Jev: watch", None, attention_move(conn, horizon_h, "watch")),
    ]
    for label, _, moves in specs:
        m = _mean(moves)
        edge = (m - base_mean) if (m is not None and base_mean is not None) else None
        checks.append({"label": label, "n": len(moves), "mean": m, "edge": edge})
    return {"horizon_h": horizon_h, "baseline_n": len(base), "baseline_mean": base_mean, "checks": checks}


def report(res, min_samples):
    print(f"Горизонт: {res['horizon_h']}ч вперёд")
    if res["baseline_mean"] is None:
        print("Пока нет пар «сигнал → цена спустя горизонт». Нужно больше прогонов с --log,")
        print("разнесённых во времени. Загляни через день-другой накопления.")
        return
    print(f"База (случайная монета): среднее {res['baseline_mean']:+.2f}% (n={res['baseline_n']})\n")
    print(f"{'сигнал':<20}{'n':>5}{'ср.движение':>14}{'EDGE vs база':>16}")
    for c in res["checks"]:
        if c["n"] < min_samples:
            print(f"{c['label']:<20}{c['n']:>5}   мало данных (нужно ≥{min_samples})")
            continue
        edge = f"{c['edge']:+.2f}%" if c["edge"] is not None else "—"
        print(f"{c['label']:<20}{c['n']:>5}{c['mean']:>13.2f}%{edge:>16}")
    print("\nEDGE > 0 значит сигнал бил случайный выбор на этих данных. EDGE около 0 или")
    print("отрицательный — сигнал НЕ даёт преимущества, не взвешивай его сильнее прочих.")
    print("Одно среднее — не доказательство: нужен объём и проверка значимости.")


def summary_text(res, min_samples=20):
    """Compact HTML for the bot /stats command."""
    lines = [f"<b>📈 Самопроверка ({res['horizon_h']}ч вперёд)</b>"]
    if res["baseline_mean"] is None:
        lines.append("Пока мало данных — нужно больше прогонов, разнесённых во времени. "
                     "Система копит историю; загляни через день-два.")
        return "\n".join(lines)
    lines.append(f"База (случайная монета): {res['baseline_mean']:+.2f}% (n={res['baseline_n']})\n")
    any_verdict = False
    for c in res["checks"]:
        if c["n"] < min_samples:
            lines.append(f"<code>{c['label']:<16}</code> n={c['n']} — мало данных")
            continue
        any_verdict = True
        edge = c["edge"]
        mark = "✅" if (edge or 0) > 0.5 else ("➖" if abs(edge or 0) <= 0.5 else "❌")
        lines.append(f"{mark} <code>{c['label']:<16}</code> edge {edge:+.2f}% (n={c['n']})")
    if any_verdict:
        lines.append("\n<i>EDGE>0 = сигнал бил случайность на этих данных. "
                     "Чем больше n, тем надёжнее.</i>")
    else:
        lines.append("\n<i>Вердиктов пока нет — копим данные (`--log` идёт автоматически).</i>")
    return "\n".join(lines)


def selftest():
    import time as _t
    conn = history.connect(":memory:")
    now = int(_t.time())
    # два момента: t0 (сигнал oversold, цена 100) и t0+25ч (цена 110 = +10%)
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now, "BTC", 100.0, "oversold"))
    conn.execute("INSERT INTO scout_runs (ts,symbol,price,signal) VALUES (?,?,?,?)", (now + 25*3600, "BTC", 110.0, None))
    conn.execute("INSERT INTO attention_runs (ts,symbol,attention,action,act_conf) VALUES (?,?,?,?,?)",
                 (now, "BTC", 2.0, "research", 0.9))
    conn.commit()
    res = compute(conn, horizon_h=24)
    osig = next(c for c in res["checks"] if c["label"].startswith("oversold"))
    assert osig["n"] == 1 and abs(osig["mean"] - 10.0) < 1e-6, osig
    attn = next(c for c in res["checks"] if c["label"] == "Jev: research")
    assert attn["n"] == 1 and abs(attn["mean"] - 10.0) < 1e-6, attn
    assert "Самопроверка" in summary_text(res)
    print("validate selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--horizon-hours", type=int, default=24)
    p.add_argument("--min-samples", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest:
        selftest()
    else:
        conn = history.connect()
        total = conn.execute("SELECT COUNT(*) FROM scout_runs").fetchone()[0]
        if total == 0:
            print("history.db пуст. Прогоны с --log идут автоматически (бот/таймер) — "
                  "загляни сюда через день-два накопления.")
        else:
            res = compute(conn, args.horizon_hours)
            if args.json:
                import json
                print(json.dumps(res, ensure_ascii=False))
            else:
                print(f"{total} записей scout в истории.\n")
                report(res, args.min_samples)
        conn.close()

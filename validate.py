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


def _var(xs, m):
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1) if len(xs) > 1 else 0.0


def _ztest(sig, base):
    """Two-sample z of (mean_sig - mean_base). |z|>1.96 ≈ p<0.05: the edge is
    unlikely to be random. No scipy — plain formula, and with crypto's fat
    tails treat it as a rough guide, not gospel. Returns (z, significant)."""
    if len(sig) < 5 or len(base) < 5:
        return None, False
    ms, mb = _mean(sig), _mean(base)
    se = (_var(sig, ms) / len(sig) + _var(base, mb) / len(base)) ** 0.5
    if se == 0:
        return None, False
    z = (ms - mb) / se
    return z, abs(z) > 1.96


def compute(conn, horizon_h=24):
    base = baseline_move(conn, horizon_h)
    base_mean = _mean(base)
    checks = []
    specs = [
        ("oversold (RSI)", signal_move(conn, horizon_h, "signal='oversold'")),
        ("overbought (RSI)", signal_move(conn, horizon_h, "signal='overbought'")),
        ("Jev: research", attention_move(conn, horizon_h, "research")),
        ("Jev: watch", attention_move(conn, horizon_h, "watch")),
    ]
    for label, moves in specs:
        m = _mean(moves)
        edge = (m - base_mean) if (m is not None and base_mean is not None) else None
        z, sig = _ztest(moves, base)
        checks.append({"label": label, "n": len(moves), "mean": m, "edge": edge,
                       "z": z, "significant": sig})
    return {"horizon_h": horizon_h, "baseline_n": len(base), "baseline_mean": base_mean, "checks": checks}


def report(res, min_samples):
    print(f"Горизонт: {res['horizon_h']}ч вперёд")
    if res["baseline_mean"] is None:
        print("Пока нет пар «сигнал → цена спустя горизонт». Нужно больше прогонов с --log,")
        print("разнесённых во времени. Загляни через день-другой накопления.")
        return
    print(f"База (случайная монета): среднее {res['baseline_mean']:+.2f}% (n={res['baseline_n']})\n")
    print(f"{'сигнал':<20}{'n':>5}{'EDGE vs база':>14}{'z':>8}{'значимо':>9}")
    for c in res["checks"]:
        if c["n"] < min_samples:
            print(f"{c['label']:<20}{c['n']:>5}   мало данных (нужно ≥{min_samples})")
            continue
        edge = f"{c['edge']:+.2f}%" if c["edge"] is not None else "—"
        zs = f"{c['z']:+.2f}" if c["z"] is not None else "—"
        sig = "ДА ✓" if c["significant"] else "нет"
        print(f"{c['label']:<20}{c['n']:>5}{edge:>14}{zs:>8}{sig:>9}")
    print("\nEDGE > 0 = сигнал бил случайный выбор. Но EDGE без значимости — это ещё")
    print("не преимущество: |z|>1.96 (значимо ✓) означает, что перевес вряд ли случаен")
    print("(~95%). Пока «значимо = нет» — доверять сигналу как эджу рано, сколько бы")
    print("ни было среднее. У крипты толстые хвосты — даже значимость тут ориентир, не гарантия.")


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
        edge = c["edge"] or 0
        # значимость решает: зелёная галка только если эдж И статзначим
        if c["significant"] and edge > 0:
            mark = "✅"
        elif c["significant"] and edge < 0:
            mark = "❌"
        else:
            mark = "➖"  # не значимо — пока шум, не доверять
        zs = f", z={c['z']:+.1f}" if c["z"] is not None else ""
        lines.append(f"{mark} <code>{c['label']:<16}</code> edge {edge:+.2f}%{zs} (n={c['n']})")
    if any_verdict:
        lines.append("\n<i>✅ = эдж есть И он статистически значим (|z|&gt;1.96, ~95%). "
                     "➖ = перевес пока может быть случайным, доверять рано.</i>")
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
    assert osig["significant"] is False, "n=1 can't be significant"  # z-test needs n>=5
    attn = next(c for c in res["checks"] if c["label"] == "Jev: research")
    assert attn["n"] == 1 and abs(attn["mean"] - 10.0) < 1e-6, attn
    assert "Самопроверка" in summary_text(res)
    # z-test math: two well-separated spread samples are significant
    sig_sample = [4, 5, 6, 5, 4, 6, 5, 4, 6, 5]   # mean 5
    base_sample = [-1, 0, 1, 0, -1, 1, 0, -1, 1, 0]  # mean 0
    z, sig = _ztest(sig_sample, base_sample)
    assert sig and z and z > 2, (z, sig)
    z2, sig2 = _ztest([1, 2, 3], [1, 2, 3])  # too few
    assert not sig2
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

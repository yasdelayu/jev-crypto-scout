"""Технические осцилляторы — чистая арифметика, без Jev и без внешних библиотек.

Ровно та же логика, что в quant_signals(): числа считает код. TradingView зовёт
это "Oscillators" — RSI, MACD, Bollinger %B, Stochastic %K — стандартные формулы,
каждая ~10 строк, никакого TA-Lib не нужно.
"""


def ema_series(values, period):
    """Экспоненциальное скользящее среднее. Первое значение — SMA разгона."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=14):
    """Wilder's RSI. 0 = всё падало, 100 = всё росло. None, если истории мало."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def macd_histogram(closes, fast=12, slow=26, signal=9):
    """Гистограмма MACD: разница между MACD-линией и её сигнальной EMA.
    Положительная и растущая — восходящий импульс набирает силу."""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    # выравниваем по длине (ema_slow короче, т.к. period больше)
    macd_line = [f - s for f, s in zip(ema_fast[-len(ema_slow):], ema_slow)]
    sig = ema_series(macd_line, signal)
    if not sig:
        return None
    return round(macd_line[-1] - sig[-1], 4)


def bollinger_percent_b(closes, period=20, num_std=2):
    """Где цена относительно полос Боллинджера: 0 — у нижней, 1 — у верхней,
    может выходить за 0..1 при пробое. None при нулевой волатильности."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((c - mean) ** 2 for c in window) / period
    std = variance ** 0.5
    if std == 0:
        return None
    upper, lower = mean + num_std * std, mean - num_std * std
    return round((closes[-1] - lower) / (upper - lower), 3)


def stochastic_k(highs, lows, closes, period=14):
    """%K: позиция последнего закрытия в диапазоне high/low за период, 0..100."""
    if len(closes) < period:
        return None
    hh, ll = max(highs[-period:]), min(lows[-period:])
    if hh == ll:
        return None
    return round((closes[-1] - ll) / (hh - ll) * 100, 1)


def read_signal(rsi_v, stoch_v):
    """Простая пороговая метка — код, не Jev: перекупленность/перепроданность
    определяются числом, а не суждением, спрашивать модель тут нечего."""
    if rsi_v is None:
        return ""
    if rsi_v < 30 or (stoch_v is not None and stoch_v < 20):
        return "oversold"
    if rsi_v > 70 or (stoch_v is not None and stoch_v > 80):
        return "overbought"
    return ""


def selftest():
    rising = [100 + i for i in range(30)]
    falling = [130 - i for i in range(30)]
    assert rsi(rising, 14) == 100.0
    assert rsi(falling, 14) == 0.0
    assert rsi([100] * 20) is None or rsi([100] * 20) in (0.0, 100.0)  # плоская серия — вырожденный случай
    assert rsi([1, 2]) is None  # истории мало

    # линейный тренд даёт константный MACD, сигнальная EMA его догоняет и гистограмма
    # сходится к нулю — не годится как тест. Постоянный % роста/падения (степенной
    # ряд) тоже не подходит: абсолютный шаг падения тогда СНИЖАЕТСЯ — это замедление,
    # а не ускорение. Нужен по-настоящему растущий по модулю шаг — квадратичный ряд.
    accelerating_up = [100 + i ** 2 * 0.5 for i in range(30)]
    accelerating_down = [1000 - i ** 2 * 0.5 for i in range(30)]
    hist_up = macd_histogram(accelerating_up, fast=3, slow=6, signal=2)
    hist_down = macd_histogram(accelerating_down, fast=3, slow=6, signal=2)
    assert hist_up is not None and hist_up > 0, hist_up
    assert hist_down is not None and hist_down < 0, hist_down
    assert macd_histogram([1, 2, 3]) is None

    flat = [50.0] * 25
    assert bollinger_percent_b(flat) is None  # нулевая волатильность
    volatile = [50 + (5 if i % 2 == 0 else -5) for i in range(25)]
    b = bollinger_percent_b(volatile)
    assert b is not None and 0 <= b <= 1, b

    highs = [10] * 13 + [20]
    lows = [5] * 13 + [5]
    closes = [7] * 13 + [20]
    assert stochastic_k(highs, lows, closes, period=14) == 100.0
    assert stochastic_k([5] * 14, [5] * 14, [5] * 14) is None  # hh == ll

    assert read_signal(25, None) == "oversold"
    assert read_signal(75, None) == "overbought"
    assert read_signal(50, 90) == "overbought"
    assert read_signal(50, 50) == ""
    assert read_signal(None, None) == ""
    print("indicators selftest ok")


if __name__ == "__main__":
    selftest()

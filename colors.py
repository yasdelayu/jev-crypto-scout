"""Raw ANSI colors — no `rich`/`colorama` dependency, ~15 lines does the job.
Auto-disables when stdout isn't a real terminal (piped to a file, CI log,
`--save` redirected), so scripted use never gets escape-code garbage.
"""
import sys

_ON = sys.stdout.isatty()


def _wrap(code):
    return (lambda s: f"\033[{code}m{s}\033[0m") if _ON else (lambda s: s)


mint = _wrap("38;5;114")    # 🧮 code/math accent — same role as the web dashboard's mint
amber = _wrap("38;5;179")   # 🧠 Jev accent
green = _wrap("38;5;114")   # bullish / oversold / good
red = _wrap("38;5;203")     # bearish / overbought / risk
dim = _wrap("2")
bold = _wrap("1")


def signed(value, width=0, decimals=1):
    """Color a % change green/red by sign, dash for None — used everywhere
    a number can go either way (24h/7d change, MACD histogram, funding z).
    Pads to `width` BEFORE coloring: ANSI escape bytes count toward Python's
    str length, so padding an already-colored string misaligns the table."""
    if value is None:
        return dim(f"{'—':>{width}}" if width else "—")
    s = f"{value:>+{width}.{decimals}f}" if width else f"{value:+.{decimals}f}"
    return green(s) if value >= 0 else red(s)


def pill(text, width=0):
    """oversold -> green, overbought -> red, anything else dim. Pads BEFORE
    coloring, same reason as signed() — ANSI bytes would break alignment."""
    label = {"oversold": "▾ oversold", "overbought": "▴ overbought"}.get(text, text or "—")
    padded = f"{label:>{width}}" if width else label
    if text == "oversold":
        return green(padded)
    if text == "overbought":
        return red(padded)
    return dim(padded)

"""Shared settings for the bot and the scheduled run — one JSON file so what
you change in Telegram (/top, /lang, /toggle) also applies to the next timer
run. Lives next to the code, gitignored (per-deploy state, not source).
"""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    "top": 100,          # сколько монет по капитализации
    "lang": "ru",        # язык новостей: ru | en
    "ta": True,          # осцилляторы RSI/MACD/...
    "fng": True,         # Fear&Greed + DeFi TVL
    "scalp": True,       # фандинг + минутные осцилляторы Bybit
    "listings": True,    # листинги/делистинги
    "news": True,        # разбор новостей через Jev
    "attention": True,   # Jev-подсказки «на что смотреть»
    "ta_limit": 30,      # на больших списках осцилляторы/scalp только по N подвижным
}

BOOL_KEYS = ["ta", "fng", "scalp", "listings", "news", "attention"]


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:
        pass  # нет файла или битый — берём дефолты, не падаем
    return cfg


def save(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def to_argv(cfg):
    """Config -> scout.py CLI args. This is the single place that maps stored
    settings onto flags, used by both /run and the scheduled run.sh."""
    argv = ["--top", str(cfg.get("top", 100)),
            "--ta-limit", str(cfg.get("ta_limit", 30))]
    if cfg.get("ta"):
        argv.append("--ta")
    if cfg.get("fng"):
        argv.append("--fng")
    if cfg.get("scalp"):
        argv.append("--scalp")
    if cfg.get("listings"):
        argv.append("--listings")
    if cfg.get("news"):
        argv += ["--news", "--lang", cfg.get("lang", "ru")]
    if cfg.get("attention"):
        argv.append("--attention")
        if "--lang" not in argv:  # attention тоже уважает язык, даже если news выкл
            argv += ["--lang", cfg.get("lang", "ru")]
    return argv


def selftest():
    d = dict(DEFAULTS)
    argv = to_argv(d)
    assert "--top" in argv and "100" in argv
    assert "--ta" in argv and "--scalp" in argv and "--listings" in argv
    assert "--news" in argv and "ru" in argv
    # toggling news off drops --news; --lang stays if attention still on
    d["news"] = False
    argv2 = to_argv(d)
    assert "--news" not in argv2 and "--attention" in argv2 and "--lang" in argv2
    # both off -> no --lang
    d["attention"] = False
    argv3 = to_argv(d)
    assert "--news" not in argv3 and "--attention" not in argv3 and "--lang" not in argv3
    # top respected
    d["top"] = 25
    assert "25" in to_argv(d)
    print("botconfig selftest ok")


if __name__ == "__main__":
    selftest()

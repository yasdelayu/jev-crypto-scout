"""Тонкий клиент Jev поверх трёх провайдеров. Общий модуль для calib.py и scout.py —
чтобы формат запроса/ответа не расходился между репозиториями.

Провайдер выбирается по тому, какая переменная окружения задана:
  TYPESAFE_API_KEY                              -> нативный API
  AI_GATEWAY_API_KEY                             -> Vercel AI Gateway
  CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID   -> Cloudflare Workers AI
  JEV_PROVIDER=demo                              -> открытая модель без ключа (НЕ Jev, только для дымового теста)
"""
import json, os, sys, time, urllib.error, urllib.request

# url, model, имя булева примитива, заворачивать ли тело в "input", имя поля usage-токенов
PROVIDERS = {
    "typesafe":   ("https://api.typesafe.ai/v1/systemone", "jev-latest", "noul", False, "input_tokens"),
    "vercel":     ("https://ai-gateway.vercel.sh/v1/evaluate", "typesafe-ai/jev", "boolean", False, "inputTokens"),
    "cloudflare": ("https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run",
                   "typesafe/jev", "noul", True, "input_tokens"),
    "demo":       ("https://simple-jev-demo-api.featherless.ai/v1/classifier",
                   "featherless-ai/Qwen3.8-27B-classifier", "noul", False, "input_tokens"),
}


def pick_provider():
    if os.environ.get("JEV_PROVIDER") == "demo":
        return "demo", ""
    if os.environ.get("TYPESAFE_API_KEY"):
        return "typesafe", os.environ["TYPESAFE_API_KEY"]
    if os.environ.get("AI_GATEWAY_API_KEY"):
        return "vercel", os.environ["AI_GATEWAY_API_KEY"]
    if os.environ.get("CLOUDFLARE_API_TOKEN"):
        if not os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
            sys.exit("для Cloudflare нужен ещё CLOUDFLARE_ACCOUNT_ID")
        return "cloudflare", os.environ["CLOUDFLARE_API_TOKEN"]
    sys.exit("нет ключа: задай TYPESAFE_API_KEY, AI_GATEWAY_API_KEY, "
             "CLOUDFLARE_API_TOKEN(+CLOUDFLARE_ACCOUNT_ID), или JEV_PROVIDER=demo для дымового теста")


class Jev:
    """provider, key — из pick_provider(). questions используют noul_type() для булева типа."""

    def __init__(self, provider, key):
        self.provider = provider
        self.key = key
        self.url, self.model, self.noul_type, self.wrap, self.tok_field = PROVIDERS[provider]
        self.url = self.url.format(acct=os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""))

    def ask(self, state, questions, attempts=7):
        payload = {"state": state, "questions": questions}
        body = {"model": self.model, "input": payload} if self.wrap else {"model": self.model, **payload}
        req = urllib.request.Request(
            self.url, json.dumps(body).encode(),
            {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
             "User-Agent": "jev-crypto-scout/1"})
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = json.load(r)
                out = out["result"] if "result" in out else out  # Cloudflare оборачивает ответ
                return self._normalize(out)
            except urllib.error.HTTPError as e:
                # 502/503/504 — апстрим Jev перегружен/оборвался, лечится бэкоффом
                if e.code in (429, 529, 502, 503, 504) and i < attempts - 1:
                    time.sleep(2 ** i)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode('utf8','replace')}") from None

    def _normalize(self, out):
        """Приводит булев ответ и usage к единой форме (noul/input_tokens),
        независимо от того, каким полем их назвал конкретный провайдер."""
        for a in out.get("answers", {}).values():
            if a.get("type") in ("noul", "boolean") and "noul" not in a:
                a["noul"] = a.get("probability", a.get("boolean"))
        u = out.setdefault("usage", {})
        if "input_tokens" not in u:
            u["input_tokens"] = u.get(self.tok_field, 0)
        return out

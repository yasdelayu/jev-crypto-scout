"""One HTTP GET helper for the whole repo, with retry/backoff — replaces the
two near-identical `get_json()` copies that used to live separately in
scout.py and scalp.py (DRY, and only one place to fix reliability in).
"""
import json, time, urllib.error, urllib.request

UA = {"User-Agent": "jev-crypto-scout/1"}


def get_json(url, timeout=20, attempts=3, retry_statuses=(429, 500, 502, 503, 504)):
    """GET url as JSON, retrying on transient failures with exponential
    backoff. Does NOT retry 403/404/etc — those are not transient, retrying
    them just burns more of a rate-limited free tier for the same result."""
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in retry_statuses and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise

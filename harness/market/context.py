"""Market context fetchers, ported from scripts/desk_context.py. Writes state/context.json."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .. import paths
from ..util import read_json, utc_now, write_json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REQUIRED_SOURCES = ("cnn", "crypto_fg", "coinpaprika")
PAPRIKA = {"btc-bitcoin": "BTC", "eth-ethereum": "ETH", "sol-solana": "SOL", "xrp-xrp": "XRP",
           "doge-dogecoin": "DOGE", "ada-cardano": "ADA", "link-chainlink": "LINK", "avax-avalanche": "AVAX"}


def get_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_cnn_fear_greed() -> dict | None:
    url = "https://production.dataviz.cnn.io/index/fearandgreed/current"
    headers = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
               "Referer": "https://www.cnn.com/markets/fear-and-greed", "Origin": "https://www.cnn.com"}
    try:
        raw = get_json(url, headers)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    score = raw.get("score")
    if score is None:
        return None
    return {"score": float(score), "rating": raw.get("rating") or "", "previous_close": raw.get("previous_close"),
            "timestamp": raw.get("timestamp"), "endpoint": url}


def fetch_crypto_fear_greed() -> dict | None:
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        raw = get_json(url, {"User-Agent": "trading-desk/0.4", "Accept": "application/json"})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    rows = raw.get("data") or []
    if not rows:
        return None
    try:
        value = int(rows[0].get("value"))
    except (TypeError, ValueError):
        return None
    return {"score": value, "rating": rows[0].get("value_classification") or "", "endpoint": url}


def fetch_vix() -> dict | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d"
    try:
        raw = get_json(url, {"User-Agent": UA, "Accept": "application/json"})
        price = float(raw["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, KeyError, TypeError, ValueError, IndexError):
        return None
    return {"last": price, "endpoint": url}


def fetch_coinpaprika() -> list[dict]:
    out = []
    headers = {"User-Agent": "trading-desk/0.4", "Accept": "application/json"}
    for pid, symbol in PAPRIKA.items():
        url = f"https://api.coinpaprika.com/v1/tickers/{pid}?quotes=USD"
        try:
            q = (get_json(url, headers).get("quotes") or {}).get("USD") or {}
            out.append({"symbol": symbol, "price": q.get("price"), "change": q.get("percent_change_24h"),
                        "change7d": q.get("percent_change_7d"), "endpoint": url})
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return out


def build_signals(cnn, crypto_fg, vix, quotes) -> list[dict]:
    signals = []
    if cnn:
        score = cnn["score"]
        sev = "high" if score <= 25 else ("medium" if score <= 45 else "low")
        signals.append({"id": "cnn:fear-greed", "type": "CNN_FEAR_GREED", "theater": "US equities",
                        "summary": f"CNN Fear & Greed {score:.0f} ({cnn.get('rating') or ''})", "severity": sev,
                        "endpoint": cnn.get("endpoint")})
    if crypto_fg:
        score = crypto_fg["score"]
        sev = "high" if (score <= 25 or score >= 75) else ("medium" if (score <= 45 or score >= 60) else "low")
        signals.append({"id": "crypto:fear-greed", "type": "CRYPTO_FEAR_GREED", "theater": "Crypto",
                        "summary": f"Crypto Fear & Greed {score} ({crypto_fg.get('rating') or ''})", "severity": sev,
                        "endpoint": crypto_fg.get("endpoint")})
    if vix:
        last = vix["last"]
        sev, kind = "low", "VIX"
        if last >= 30:
            sev, kind = "high", "VIX_SPIKE"
        elif last >= 20:
            sev, kind = "medium", "VIX_ELEVATED"
        signals.append({"id": "yahoo:vix", "type": kind, "theater": "Global Markets", "summary": f"VIX {last:.2f}",
                        "severity": sev, "endpoint": vix.get("endpoint")})
    for q in quotes:
        chg = q.get("change")
        if chg is None or abs(float(chg)) < 8:
            continue
        signals.append({"id": f"paprika:{q['symbol']}", "type": "CRYPTO_MOVE", "theater": "Crypto",
                        "summary": f"{q['symbol']} 24h {float(chg):+.1f}%",
                        "severity": "medium" if abs(float(chg)) < 15 else "high", "endpoint": q.get("endpoint")})
    return signals


def context_health(errors: list[str]) -> tuple[bool, bool]:
    required_failed = [n for n in REQUIRED_SOURCES if n in errors]
    return bool(required_failed), len(required_failed) == len(REQUIRED_SOURCES)


def fetch_all() -> dict:
    errors = []
    cnn = fetch_cnn_fear_greed()
    if cnn is None:
        errors.append("cnn")
    crypto_fg = fetch_crypto_fear_greed()
    if crypto_fg is None:
        errors.append("crypto_fg")
    vix = fetch_vix()
    if vix is None:
        errors.append("vix")
    quotes = fetch_coinpaprika()
    if not quotes:
        errors.append("coinpaprika")
    degraded, unavailable = context_health(errors)
    return {"fetchedAt": utc_now(), "degraded": degraded, "unavailable": unavailable, "errors": errors, "cnn": cnn,
            "cryptoFearGreed": crypto_fg, "vix": vix, "quotes": quotes,
            "signals": build_signals(cnn, crypto_fg, vix, quotes)}


def context_path() -> Path:
    return paths.state_dir() / "context.json"


def refresh() -> dict:
    payload = fetch_all()
    write_json(context_path(), payload)
    return payload


def load() -> dict | None:
    fake = os.environ.get("DESK_FAKE_CONTEXT", "").strip()
    if fake:
        return read_json(Path(fake))
    return read_json(context_path())

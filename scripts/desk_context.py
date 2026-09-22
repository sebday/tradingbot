#!/usr/bin/env python3
"""Desk context: CNN fear/greed, crypto F&G, VIX, CoinPaprika. No WorldMonitor UI."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path(os.environ.get("TRADING_DESK_LEDGER") or Path.home() / ".local/state/omarchy/trading/ledger")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Yahoo VIX is optional. A dead chart is logged in errors and does not degrade context.
REQUIRED_SOURCES = ("cnn", "crypto_fg", "coinpaprika")

PAPRIKA = {
    "btc-bitcoin": "BTC",
    "eth-ethereum": "ETH",
    "sol-solana": "SOL",
    "xrp-xrp": "XRP",
    "doge-dogecoin": "DOGE",
    "ada-cardano": "ADA",
    "link-chainlink": "LINK",
    "avax-avalanche": "AVAX",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_json(url: str, headers: dict[str, str], timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_cnn_fear_greed() -> dict | None:
    url = "https://production.dataviz.cnn.io/index/fearandgreed/current"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.cnn.com/markets/fear-and-greed",
        "Origin": "https://www.cnn.com",
    }
    try:
        raw = get_json(url, headers)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    score = raw.get("score")
    if score is None:
        return None
    return {
        "score": float(score),
        "rating": raw.get("rating") or "",
        "previous_close": raw.get("previous_close"),
        "timestamp": raw.get("timestamp"),
        "endpoint": url,
    }


def fetch_crypto_fear_greed() -> dict | None:
    url = "https://api.alternative.me/fng/?limit=1"
    try:
        raw = get_json(url, {"User-Agent": "trading-desk/0.4", "Accept": "application/json"})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    rows = raw.get("data") or []
    if not rows:
        return None
    row = rows[0]
    try:
        value = int(row.get("value"))
    except (TypeError, ValueError):
        return None
    return {
        "score": value,
        "rating": row.get("value_classification") or "",
        "endpoint": url,
    }


def fetch_vix() -> dict | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d"
    try:
        raw = get_json(url, {"User-Agent": UA, "Accept": "application/json"})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, KeyError):
        return None
    try:
        meta = raw["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return {"last": price, "endpoint": url}


def fetch_coinpaprika() -> list[dict]:
    out = []
    headers = {"User-Agent": "trading-desk/0.4", "Accept": "application/json"}
    for pid, symbol in PAPRIKA.items():
        url = f"https://api.coinpaprika.com/v1/tickers/{pid}?quotes=USD"
        try:
            raw = get_json(url, headers)
            q = (raw.get("quotes") or {}).get("USD") or {}
            out.append(
                {
                    "symbol": symbol,
                    "price": q.get("price"),
                    "change": q.get("percent_change_24h"),
                    "change7d": q.get("percent_change_7d"),
                    "endpoint": url,
                }
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return out


def build_signals(
    cnn: dict | None,
    crypto_fg: dict | None,
    vix: dict | None,
    quotes: list[dict],
) -> list[dict]:
    signals = []
    if cnn:
        score = cnn["score"]
        rating = cnn.get("rating") or ""
        severity = "low"
        if score <= 25:
            severity = "high"
        elif score <= 45:
            severity = "medium"
        signals.append(
            {
                "id": "cnn:fear-greed",
                "type": "CNN_FEAR_GREED",
                "theater": "US equities",
                "summary": f"CNN Fear & Greed {score:.0f} ({rating})",
                "severity": severity,
                "endpoint": cnn.get("endpoint"),
            }
        )
    if crypto_fg:
        score = crypto_fg["score"]
        rating = crypto_fg.get("rating") or ""
        severity = "low"
        if score <= 25 or score >= 75:
            severity = "high"
        elif score <= 45 or score >= 60:
            severity = "medium"
        signals.append(
            {
                "id": "crypto:fear-greed",
                "type": "CRYPTO_FEAR_GREED",
                "theater": "Crypto",
                "summary": f"Crypto Fear & Greed {score} ({rating})",
                "severity": severity,
                "endpoint": crypto_fg.get("endpoint"),
            }
        )
    if vix:
        last = vix["last"]
        severity = "low"
        kind = "VIX"
        if last >= 30:
            severity, kind = "high", "VIX_SPIKE"
        elif last >= 20:
            severity, kind = "medium", "VIX_ELEVATED"
        signals.append(
            {
                "id": "yahoo:vix",
                "type": kind,
                "theater": "Global Markets",
                "summary": f"VIX {last:.2f}",
                "severity": severity,
                "endpoint": vix.get("endpoint"),
            }
        )
    for q in quotes:
        chg = q.get("change")
        if chg is None:
            continue
        if abs(float(chg)) < 8:
            continue
        signals.append(
            {
                "id": f"paprika:{q['symbol']}",
                "type": "CRYPTO_MOVE",
                "theater": "Crypto",
                "summary": f"{q['symbol']} 24h {float(chg):+.1f}%",
                "severity": "medium" if abs(float(chg)) < 15 else "high",
                "endpoint": q.get("endpoint"),
            }
        )
    return signals


def context_health(errors: list[str]) -> tuple[bool, bool]:
    """Return (degraded, unavailable). A dead Yahoo VIX does not count."""
    required_failed = [name for name in REQUIRED_SOURCES if name in errors]
    unavailable = len(required_failed) == len(REQUIRED_SOURCES)
    degraded = bool(required_failed)
    return degraded, unavailable


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
    payload = {
        "fetchedAt": utc_now(),
        "degraded": degraded,
        "unavailable": unavailable,
        "errors": errors,
        "cnn": cnn,
        "cryptoFearGreed": crypto_fg,
        "vix": vix,
        "quotes": quotes,
        "signals": build_signals(cnn, crypto_fg, vix, quotes),
    }
    return payload


def write_context(payload: dict) -> Path:
    LEDGER.mkdir(parents=True, exist_ok=True)
    path = LEDGER / "context.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def main() -> int:
    payload = fetch_all()
    write_context(payload)
    print(
        json.dumps(
            {
                "status": "DOWN" if payload["unavailable"] else ("DEGRADED" if payload["degraded"] else "READY"),
                "fetchedAt": payload["fetchedAt"],
                "signals": len(payload["signals"]),
                "quotes": len(payload["quotes"]),
                "errors": payload["errors"],
            },
            indent=2,
        )
    )
    return 1 if payload["unavailable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

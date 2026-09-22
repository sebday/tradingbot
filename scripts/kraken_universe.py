#!/usr/bin/env python3
"""Kraken spot universe: online pairs in the desk quote currency. No API key."""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KRAKEN = "https://api.kraken.com/0/public"
UA = "trading-desk-paper/0.2"


def ledger_dir() -> Path:
    return Path(os.environ.get("TRADING_DESK_LEDGER") or Path.home() / ".local/state/omarchy/trading/ledger")


def cache_path() -> Path:
    return ledger_dir() / "universe-kraken.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text())


def get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def kraken(path: str) -> dict:
    data = get_json(f"{KRAKEN}/{path}")
    errors = data.get("error") or []
    if errors:
        raise RuntimeError(f"kraken {path}: {errors}")
    return data.get("result") or {}


def quote_aliases(currency: str) -> set[str]:
    c = currency.upper().strip()
    aliases = {c, f"Z{c}"}
    if c == "BTC":
        aliases.update({"XBT", "XXBT"})
    return aliases


def taker_fee_fraction(info: dict, fallback: float) -> float:
    fees = info.get("fees") or []
    if not fees:
        return fallback
    try:
        pct = float(fees[0][1])
        return pct / 100.0 if pct > 0.05 else pct
    except (TypeError, ValueError, IndexError):
        return fallback


def is_usd_quote(quote: str, aliases: set[str]) -> bool:
    q = (quote or "").upper()
    return q in aliases


def fetch_universe(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    currency = cfg.get("bank", {}).get("currency", "USD")
    aliases = quote_aliases(currency)
    fallback_fee = float(cfg.get("fee", {}).get("rate", 0.004))
    pairs = kraken("AssetPairs")
    universe = []
    for pair_id, info in pairs.items():
        if pair_id.endswith(".d"):
            continue
        status = (info.get("status") or "online").lower()
        if status != "online":
            continue
        if not is_usd_quote(str(info.get("quote") or ""), aliases):
            continue
        ordermin = info.get("ordermin")
        if ordermin in (None, ""):
            continue
        universe.append(
            {
                "pair_id": pair_id,
                "altname": info.get("altname") or pair_id,
                "wsname": info.get("wsname") or "",
                "base": info.get("base") or "",
                "quote": info.get("quote") or "",
                "ordermin": str(ordermin),
                "costmin": str(info["costmin"]) if info.get("costmin") not in (None, "") else None,
                "pair_decimals": info.get("pair_decimals"),
                "lot_decimals": info.get("lot_decimals"),
                "taker_fee": taker_fee_fraction(info, fallback_fee),
            }
        )
    universe.sort(key=lambda p: p["wsname"] or p["altname"])
    payload = {
        "fetchedAt": utc_now(),
        "currency": currency,
        "count": len(universe),
        "pairs": universe,
    }
    LEDGER = ledger_dir()
    LEDGER.mkdir(parents=True, exist_ok=True)
    cache_path().write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def load_universe(refresh: bool = False, cfg: dict | None = None) -> dict:
    if refresh or not cache_path().exists():
        return fetch_universe(cfg)
    return json.loads(cache_path().read_text())


def pair_index(universe: dict) -> dict[str, dict]:
    return {p["pair_id"]: p for p in universe.get("pairs") or []}


def ticker_metrics(tick: dict) -> dict:
    ask = float(tick["a"][0])
    bid = float(tick["b"][0])
    last = float(tick["c"][0])
    vol_base_24h = float(tick["v"][1])
    trades_24h = int(tick["t"][1])
    open_px = float(tick["o"])
    mid = (ask + bid) / 2.0 if ask and bid else last
    spread = ((ask - bid) / mid) if mid else None
    pct_change = ((last - open_px) / open_px) if open_px else None
    volume_24h_quote = vol_base_24h * last
    return {
        "ask": ask,
        "bid": bid,
        "last": last,
        "mid": mid,
        "spread": spread,
        "pct_change": pct_change,
        "volume_base_24h": vol_base_24h,
        "volume_24h_quote": volume_24h_quote,
        "trades_24h": trades_24h,
    }


def fetch_tickers(pair_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    chunk = 40
    for i in range(0, len(pair_ids), chunk):
        batch = pair_ids[i : i + chunk]
        result = kraken("Ticker?pair=" + ",".join(batch))
        for pair_id, tick in result.items():
            out[pair_id] = ticker_metrics(tick)
    return out


def fetch_ohlc(pair_id: str, interval: int = 60) -> list[list]:
    result = kraken(f"OHLC?pair={pair_id}&interval={interval}")
    for key, value in result.items():
        if key == "last":
            continue
        return value
    return []


def min_ticket_usd(c: dict) -> float:
    last = float(c["last"] or 0)
    by_order = float(c["ordermin"]) * last
    by_cost = float(c["costmin"]) if c.get("costmin") else 0.0
    return max(by_order, by_cost)


def round_quote_usd(dollars: float) -> str:
    return f"{max(0.0, dollars):.2f}"


def round_base(qty: float, lot_decimals: int | None) -> str:
    decimals = int(lot_decimals if lot_decimals is not None else 8)
    q = round(float(qty), decimals)
    return f"{q:.{decimals}f}"


def volume_ratio_6h_24h(ohlc: list[list]) -> dict | None:
    if len(ohlc) < 24:
        return None
    last24 = ohlc[-24:]
    last6 = ohlc[-6:]
    vol24 = sum(float(c[6]) for c in last24)
    vol6 = sum(float(c[6]) for c in last6)
    avg_6h = vol24 / 4.0
    ratio = (vol6 / avg_6h) if avg_6h else None
    return {"volume_6h": vol6, "volume_24h": vol24, "avg_6h": avg_6h, "ratio": ratio}


if __name__ == "__main__":
    payload = fetch_universe()
    print(json.dumps({"count": payload["count"], "fetchedAt": payload["fetchedAt"]}, indent=2))

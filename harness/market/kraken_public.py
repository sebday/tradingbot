"""Kraken public data (no keys). Ported from scripts/kraken_universe.py.

Market is the live implementation; FixtureMarket replays a JSON fixture for tests
and the fake seat. Both expose: universe(), tickers(pair_ids), ohlc(pair_id, interval).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from .. import paths
from ..util import parse_ts, read_json, utc_now, write_json

KRAKEN = "https://api.kraken.com/0/public"
UA = "trading-desk-harness/0.1"


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


def parse_universe(pairs: dict, cfg: dict) -> dict:
    currency = (cfg.get("bank") or {}).get("currency", "USD")
    aliases = quote_aliases(currency)
    fallback_fee = float((cfg.get("fee") or {}).get("rate", 0.004))
    universe = []
    for pair_id, info in pairs.items():
        if pair_id.endswith(".d"):
            continue
        if (info.get("status") or "online").lower() != "online":
            continue
        if str(info.get("quote") or "").upper() not in aliases:
            continue
        ordermin = info.get("ordermin")
        if ordermin in (None, ""):
            continue
        universe.append({
            "pair_id": pair_id, "altname": info.get("altname") or pair_id, "wsname": info.get("wsname") or "",
            "base": info.get("base") or "", "quote": info.get("quote") or "", "ordermin": str(ordermin),
            "costmin": str(info["costmin"]) if info.get("costmin") not in (None, "") else None,
            "pair_decimals": info.get("pair_decimals"), "lot_decimals": info.get("lot_decimals"),
            "taker_fee": taker_fee_fraction(info, fallback_fee),
        })
    universe.sort(key=lambda p: p["wsname"] or p["altname"])
    return {"fetchedAt": utc_now(), "currency": currency, "count": len(universe), "pairs": universe}


def ticker_metrics(tick: dict) -> dict:
    ask = float(tick["a"][0])
    bid = float(tick["b"][0])
    last = float(tick["c"][0])
    vol_base_24h = float(tick["v"][1])
    trades_24h = int(tick["t"][1])
    open_px = float(tick["o"])
    high_24h = float(tick["h"][1]) if tick.get("h") else None
    low_24h = float(tick["l"][1]) if tick.get("l") else None
    mid = (ask + bid) / 2.0 if ask and bid else last
    spread = ((ask - bid) / mid) if mid else None
    pct_change = ((last - open_px) / open_px) if open_px else None
    return {"ask": ask, "bid": bid, "last": last, "mid": mid, "spread": spread, "pct_change": pct_change,
            "volume_base_24h": vol_base_24h, "volume_24h_quote": vol_base_24h * last, "trades_24h": trades_24h,
            "high_24h": high_24h, "low_24h": low_24h}


def round_base(qty: float, lot_decimals) -> str:
    decimals = int(lot_decimals if lot_decimals is not None else 8)
    q = Decimal(str(qty)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
    return f"{q:.{decimals}f}"


def round_price(price: float, pair_decimals) -> str:
    decimals = int(pair_decimals if pair_decimals is not None else 8)
    q = Decimal(str(price)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
    return f"{q:.{decimals}f}"


def volume_ratio_6h_24h(ohlc: list) -> dict | None:
    if len(ohlc) < 24:
        return None
    last24 = ohlc[-24:]
    last6 = ohlc[-6:]
    vol24 = sum(float(c[6]) for c in last24)
    vol6 = sum(float(c[6]) for c in last6)
    avg_6h = vol24 / 4.0
    ratio = (vol6 / avg_6h) if avg_6h else None
    return {"volume_6h": vol6, "volume_24h": vol24, "avg_6h": avg_6h, "ratio": ratio}


class Market:
    """Live public Kraken. Caches the universe in state/universe-kraken.json (not the legacy ledger)."""

    def __init__(self, cfg: dict, cache_dir: Path | None = None):
        self.cfg = cfg
        self.cache = (cache_dir or paths.state_dir()) / "universe-kraken.json"
        self.last_ok: dict[str, str] = {}

    def universe(self, max_age_hours: float = 12.0, refresh: bool = False) -> dict:
        cached = read_json(self.cache)
        if cached and not refresh:
            ts = parse_ts(cached.get("fetchedAt"))
            if ts and time.time() - ts < max_age_hours * 3600:
                return cached
        payload = parse_universe(kraken("AssetPairs"), self.cfg)
        write_json(self.cache, payload)
        self.last_ok["universe"] = utc_now()
        return payload

    def tickers(self, pair_ids: list[str]) -> dict:
        out: dict = {}
        for i in range(0, len(pair_ids), 40):
            batch = pair_ids[i:i + 40]
            result = kraken("Ticker?pair=" + urllib.parse.quote(",".join(batch)))
            for pid, tick in result.items():
                out[pid] = ticker_metrics(tick)
        self.last_ok["ticker"] = utc_now()
        return out

    def ohlc(self, pair_id: str, interval: int = 60, since: int | None = None) -> list:
        q = f"OHLC?pair={urllib.parse.quote(pair_id)}&interval={int(interval)}"
        if since:
            q += f"&since={int(since)}"
        result = kraken(q)
        for key, value in result.items():
            if key != "last":
                self.last_ok["ohlc"] = utc_now()
                return value
        return []


class FixtureMarket:
    """Deterministic market from a fixture: {universe:{pairs:[...]}, tickers:{pid:metrics}, ohlc:{pid:[...]}}."""

    def __init__(self, data: dict):
        self.data = data
        self.last_ok = {"universe": utc_now(), "ticker": utc_now()}

    @classmethod
    def from_path(cls, path: str | Path) -> "FixtureMarket":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def universe(self, max_age_hours: float = 12.0, refresh: bool = False) -> dict:
        u = dict(self.data.get("universe") or {})
        u.setdefault("fetchedAt", utc_now())
        u.setdefault("count", len(u.get("pairs") or []))
        return u

    def tickers(self, pair_ids: list[str]) -> dict:
        t = self.data.get("tickers") or {}
        return {pid: dict(t[pid]) for pid in pair_ids if pid in t}

    def ohlc(self, pair_id: str, interval: int = 60, since: int | None = None) -> list:
        return list((self.data.get("ohlc") or {}).get(pair_id) or [])


def market_from_env(cfg: dict):
    """DESK_FAKE_MARKET=<fixture.json> swaps in FixtureMarket (tests, fake seat)."""
    import os
    fake = os.environ.get("DESK_FAKE_MARKET", "").strip()
    if fake:
        return FixtureMarket.from_path(fake)
    return Market(cfg)

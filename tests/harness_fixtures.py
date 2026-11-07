"""Shared fixtures for harness tests. No network, no keys, no LLM."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE_CONFIG = {
    "paper": False,
    "venues": {"scan": "kraken", "fills": "kraken"},
    "bank": {"startingUsd": 587.48, "allocatedUsd": 587.48, "currency": "USD", "lockedUsd": 0},
    "kraken": {"passKey": "evoshell/kraken/trading-desk-key", "passSecret": "evoshell/kraken/trading-desk-secret"},
    "maxOpenBooks": 7,
    "live": {"maxFillsPerCycle": 1, "buyOflags": "fciq"},
    "entry": {"enabled": True, "ticketPctEquity": 0.06, "minTicketUsd": 10, "maxExposurePct": 0.8, "maxNewPerDay": 3,
              "reentryCooldownHours": 24, "maxSlippageBps": 40, "vetTimeoutSeconds": 600},
    "fee": {"rate": 0.004},
    "risk": {"stopLossPct": 0.08, "staleAlertMinutes": 30, "volumeRatioClose": 0.2, "trailArmPct": 0.08,
             "trailGiveback": 0.05, "trailFloorPct": 0.03, "pollSeconds": 3600, "exitPollSeconds": 300,
             "ohlcInterval": 60, "haltDrawdown": 0.5, "haltUntilHuman": True},
    "scan": {"pollSeconds": 3600, "maxCandidates": 10, "volume24hMin": 100000, "maxSpread": 0.004, "dayMoveMin": 0,
             "dayMoveMax": 0.15, "minRange24h": 0.01, "excludeBases": ["USDT", "USDC", "EUR"]},
    "models": {"vet": "composer-2.5"},
}


def cfg(**harness):
    from harness.config import with_defaults
    raw = json.loads(json.dumps(BASE_CONFIG))
    if harness:
        raw["harness"] = harness
    return with_defaults(raw)


def tick(last, spread=0.001, pct=0.03, vol_quote=5e6, trades=5000, rng=0.05):
    ask = last * (1 + spread / 2)
    bid = last * (1 - spread / 2)
    return {"ask": ask, "bid": bid, "last": last, "mid": last, "spread": (ask - bid) / last, "pct_change": pct,
            "volume_base_24h": vol_quote / last, "volume_24h_quote": vol_quote, "trades_24h": trades,
            "high_24h": last * (1 + rng / 2), "low_24h": last * (1 - rng / 2)}


def pair(pid, base, ws, ordermin="0.0001", lot=8, dec=2):
    return {"pair_id": pid, "altname": pid, "wsname": ws, "base": base, "quote": "ZUSD", "ordermin": ordermin,
            "costmin": "0.5", "pair_decimals": dec, "lot_decimals": lot, "taker_fee": 0.004}


def ohlc(last, now=None, n=720, vol=100.0, drift=0.0):
    now = time.time() if now is None else now
    hour = int(now) - int(now) % 3600
    rows = []
    for i in range(n):
        t = hour - (n - 1 - i) * 3600
        c = last * (1 + drift * (i - n + 1))
        rows.append([t, str(c), str(c * 1.002), str(c * 0.998), str(c), str(c), str(vol), 10])
    return rows


def market_data(now=None):
    now = time.time() if now is None else now
    pairs = [pair("XXBTZUSD", "XXBT", "XBT/USD", dec=1), pair("XETHZUSD", "XETH", "ETH/USD"),
             pair("SOLUSD", "SOL", "SOL/USD", ordermin="0.02"), pair("AAAUSD", "AAA", "AAA/USD", ordermin="1", dec=4),
             pair("USDCUSD", "USDC", "USDC/USD", ordermin="5", dec=4)]
    tickers = {"XXBTZUSD": tick(70000, vol_quote=9e8), "XETHZUSD": tick(2400, vol_quote=4e8),
               "SOLUSD": tick(150, vol_quote=1e8), "AAAUSD": tick(0.5, vol_quote=5e4),
               "USDCUSD": tick(1.0, vol_quote=9e7, rng=0.001)}
    return {"universe": {"pairs": pairs}, "tickers": tickers,
            "ohlc": {pid: ohlc(t["last"], now) for pid, t in tickers.items()}}


class Env:
    """Temp state/runs/db/ledger + fake context, set via env vars."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.old = {}

    def __enter__(self):
        from harness.util import utc_now
        (self.dir / "ledger").mkdir()
        ctx = self.dir / "context.json"
        ctx.write_text(json.dumps({"fetchedAt": utc_now(), "degraded": False, "unavailable": False, "errors": [],
                                   "signals": [], "quotes": []}))
        env = {"DESK_STATE_DIR": str(self.dir / "state"), "DESK_RUNS_DIR": str(self.dir / "runs"),
               "DESK_DB": str(self.dir / "state" / "desk.db"), "DESK_LEDGER": str(self.dir / "ledger"),
               "DESK_FAKE_CONTEXT": str(ctx), "TRADING_DESK_LEDGER": str(self.dir / "ledger")}
        for k, v in env.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_ledger(self, name, rows):
        with (self.dir / "ledger" / name).open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

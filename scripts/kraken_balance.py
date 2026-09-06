#!/usr/bin/env python3
"""Query-only Kraken balance from pass. No orders. May trip the 50% human halt."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import desk_halt  # noqa: E402
import kraken_private as kp  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text())


def main() -> int:
    key, secret = kp.load_keys(CONFIG)
    try:
        raw = kp.fetch_balance(key, secret)
        trade = kp.fetch_trade_balance(key, secret, "ZUSD")
    finally:
        del key, secret

    usd, leftover = kp.split_balances(raw)
    try:
        equity = float(trade.get("eb"))
    except (TypeError, ValueError):
        equity = usd

    allocated = desk_halt.allocated_usd(CONFIG)
    floor = desk_halt.halt_floor_usd(CONFIG)
    halt_row = desk_halt.trip_if_needed(equity, CONFIG, "kraken_balance")
    already = desk_halt.is_halted()

    report = {
        "query": "Balance+TradeBalance",
        "orders": False,
        "usd_spot": round(usd, 2),
        "equivalent_usd": round(equity, 2),
        "trade_balance": {k: trade.get(k) for k in ("eb", "tb", "m", "n", "c", "v", "e", "mf")},
        "leftover_assets": leftover,
        "allocated_usd": round(allocated, 2),
        "halt_floor_usd": round(floor, 2),
        "halted": already,
        "halt": halt_row,
        "isolation_ok": usd >= allocated * 0.9 and not leftover,
        "note": "Query only. No AddOrder.",
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

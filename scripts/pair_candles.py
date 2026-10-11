#!/usr/bin/python3 -I
"""Daily OHLC candles for one Kraken spot pair. Public market data, no API key."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kraken_universe import fetch_ohlc  # noqa: E402

PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}$")
DEFAULT_DAYS = 30


def bars_from_ohlc(rows: list, days: int) -> list[dict]:
    bars = []
    for row in rows or []:
        try:
            ts = int(row[0])
            open_ = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        bars.append(
            {
                "date": day,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "value": close,
            }
        )
    if days > 0 and len(bars) > days:
        bars = bars[-days:]
    return bars


def candles_payload(pair_id: str, days: int = DEFAULT_DAYS) -> dict:
    pair = str(pair_id or "").strip().upper()
    span = DEFAULT_DAYS if not isinstance(days, int) else days
    span = max(5, min(span, 90))
    if not PAIR_RE.match(pair):
        return {"ok": False, "error": "Bad pair", "pair_id": pair, "bars": []}
    try:
        rows = fetch_ohlc(pair, 1440) or []
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180], "pair_id": pair, "bars": []}
    return {"ok": True, "pair_id": pair, "bars": bars_from_ohlc(rows, span)}


def main() -> int:
    pair = sys.argv[1] if len(sys.argv) > 1 else ""
    days = DEFAULT_DAYS
    if len(sys.argv) > 2:
        try:
            days = int(sys.argv[2])
        except ValueError:
            days = DEFAULT_DAYS
    print(json.dumps(candles_payload(pair, days), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

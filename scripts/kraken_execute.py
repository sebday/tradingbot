#!/usr/bin/env python3
"""Live Kraken market buy/sell. Seats call this. They never read pass."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import desk_halt  # noqa: E402
import kraken_private as kp  # noqa: E402
import kraken_universe as ku  # noqa: E402

LEDGER = Path(os.environ.get("TRADING_DESK_LEDGER") or ROOT / "ledger")
CONFIG = json.loads((ROOT / "config.json").read_text())
DUST_USD = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append(name: str, row: dict) -> None:
    path = LEDGER / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def load_jsonl(name: str) -> list[dict]:
    path = LEDGER / name
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def paper_mode() -> bool:
    return bool(CONFIG.get("paper"))


def fills_venue() -> str:
    if paper_mode():
        return "paper"
    return (CONFIG.get("venues") or {}).get("fills") or "paper"


def closed_keys() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for row in load_jsonl("closes.jsonl"):
        pair_id = row.get("pair_id")
        if pair_id:
            out.add((pair_id, row.get("venue") or "paper"))
    return out


def open_fills(venue: str) -> list[dict]:
    closed = closed_keys()
    opens = []
    for row in load_jsonl("fills.jsonl"):
        pair_id = row.get("pair_id")
        fill_venue = row.get("venue") or "paper"
        if pair_id and fill_venue == venue and (pair_id, fill_venue) not in closed:
            opens.append(row)
    return opens


def wait_order(key: str, secret: str, txid: str) -> dict:
    last: dict = {}
    for _ in range(8):
        time.sleep(0.4)
        last = kp.query_orders(key, secret, txid)
        info = last.get(txid) or (next(iter(last.values())) if last else {})
        status = (info.get("status") or "").lower()
        if status in {"closed", "canceled", "expired"}:
            return info
    return last.get(txid) or (next(iter(last.values())) if last else last)


def require_live() -> None:
    if paper_mode() or fills_venue() != "kraken":
        raise SystemExit("refusing: config is paper, not live kraken fills")


def cycle_fill_count(cycle_id: str) -> int:
    n = 0
    for row in load_jsonl("fills.jsonl"):
        if row.get("cycle_id") == cycle_id and (row.get("venue") or "paper") == "kraken":
            n += 1
    return n


def cmd_buy(args: argparse.Namespace) -> int:
    require_live()
    if desk_halt.is_halted():
        print(json.dumps({"error": "HALTED", "fills": 0}), file=sys.stderr)
        return 2
    max_per = int((CONFIG.get("live") or {}).get("maxFillsPerCycle") or 1)
    if cycle_fill_count(args.cycle_id) >= max_per:
        print(json.dumps({"error": "maxFillsPerCycle", "max": max_per}), file=sys.stderr)
        return 1
    dollars = float(args.dollars)
    if dollars <= 0:
        print(json.dumps({"error": "dollars must be > 0"}), file=sys.stderr)
        return 1
    key, secret = kp.load_keys(CONFIG)
    try:
        volume = ku.round_quote_usd(dollars)
        oflags = ((CONFIG.get("live") or {}).get("oflags")) or "viqc,fciq"
        result = kp.add_order(
            key,
            secret,
            pair=args.pair,
            side="buy",
            volume=volume,
            oflags=oflags,
            validate=False,
        )
        txids = result.get("txid") or []
        txid = txids[0] if txids else None
        info = wait_order(key, secret, txid) if txid else {}
    finally:
        del key, secret
    last = float(args.decision_price or 0)
    fill_px = float(info.get("price") or last or 0)
    cost = float(info.get("cost") or volume)
    slip = 0 if not last else int(round((fill_px - last) / last * 10000))
    row = {
        "ts": utc_now(),
        "cycle_id": args.cycle_id,
        "pair_id": args.pair,
        "wsname": args.wsname or "",
        "requested": round(dollars, 2),
        "filled": round(cost, 2),
        "decision_price": last or None,
        "fill_price": fill_px,
        "slippage_bps": slip,
        "fee_percent": float((CONFIG.get("fee") or {}).get("rate") or 0),
        "fee_usd": float(info.get("fee") or 0),
        "partial": bool(info.get("status") and str(info.get("status")).lower() != "closed"),
        "venue": "kraken",
        "txid": txid,
        "volume_base": float(info.get("vol_exec") or 0),
        "descr": (result.get("descr") or {}).get("order"),
    }
    append("fills.jsonl", row)
    print(json.dumps(row, indent=2))
    return 0


def merged_open(pair_id: str) -> dict | None:
    same = [p for p in open_fills("kraken") if p["pair_id"] == pair_id]
    if not same:
        return None
    qty = sum(float(p.get("volume_base") or 0) for p in same)
    return {
        "pair_id": pair_id,
        "wsname": same[0].get("wsname") or "",
        "volume_base": qty,
        "fills": same,
    }


def cmd_sell(args: argparse.Namespace) -> int:
    require_live()
    universe = ku.load_universe()
    index = ku.pair_index(universe)
    meta = index.get(args.pair) or {}
    pos = merged_open(args.pair)
    qty = float(args.volume_base or 0)
    if qty <= 0 and pos:
        qty = float(pos["volume_base"] or 0)
    if qty <= 0:
        print(json.dumps({"error": "missing volume_base", "pair_id": args.pair}), file=sys.stderr)
        return 1
    last = None
    ticks = ku.fetch_tickers([args.pair])
    if args.pair in ticks:
        last = ticks[args.pair].get("last")
    if last and qty * float(last) < DUST_USD:
        print(json.dumps({"skipped": "dust", "pair_id": args.pair, "notional": qty * float(last)}))
        return 0
    key, secret = kp.load_keys(CONFIG)
    try:
        volume = ku.round_base(qty, meta.get("lot_decimals"))
        result = kp.add_order(
            key,
            secret,
            pair=args.pair,
            side="sell",
            volume=volume,
            oflags="fciq",
            validate=False,
        )
        txids = result.get("txid") or []
        txid = txids[0] if txids else None
        info = wait_order(key, secret, txid) if txid else {}
    finally:
        del key, secret
    row = {
        "ts": utc_now(),
        "cycle_id": args.cycle_id,
        "pair_id": args.pair,
        "wsname": args.wsname or (pos or {}).get("wsname") or "",
        "action": "CLOSE",
        "rule_fired": args.rule or "MANUAL",
        "venue": "kraken",
        "txid": txid,
        "fill_price": float(info.get("price") or 0),
        "filled": float(info.get("cost") or 0),
        "volume_base": float(info.get("vol_exec") or qty),
        "status": info.get("status"),
    }
    append("closes.jsonl", row)
    print(json.dumps(row, indent=2))
    return 0


def cmd_opens(_args: argparse.Namespace) -> int:
    venue = fills_venue()
    opens = open_fills(venue)
    by_pair: dict[str, dict] = {}
    for row in opens:
        pid = row["pair_id"]
        slot = by_pair.setdefault(
            pid,
            {
                "pair_id": pid,
                "wsname": row.get("wsname"),
                "venue": venue,
                "filled_usd": 0.0,
                "volume_base": 0.0,
                "n": 0,
            },
        )
        slot["filled_usd"] += float(row.get("filled") or 0)
        slot["volume_base"] += float(row.get("volume_base") or 0)
        slot["n"] += 1
    print(json.dumps({"venue": venue, "paper": paper_mode(), "opens": list(by_pair.values())}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Kraken execute for FILLS/RISK")
    sub = parser.add_subparsers(dest="cmd", required=True)

    buy = sub.add_parser("buy")
    buy.add_argument("--pair", required=True)
    buy.add_argument("--wsname", default="")
    buy.add_argument("--dollars", required=True, type=float)
    buy.add_argument("--decision-price", type=float, default=0)
    buy.add_argument("--cycle-id", required=True)
    buy.set_defaults(func=cmd_buy)

    sell = sub.add_parser("sell")
    sell.add_argument("--pair", required=True)
    sell.add_argument("--wsname", default="")
    sell.add_argument("--cycle-id", required=True)
    sell.add_argument("--rule", default="VOLUME_6H")
    sell.add_argument("--volume-base", type=float, default=0)
    sell.set_defaults(func=cmd_sell)

    opens = sub.add_parser("opens")
    opens.set_defaults(func=cmd_opens)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

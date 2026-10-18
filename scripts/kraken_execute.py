#!/usr/bin/python3 -I
"""Live Kraken buy/sell. The only AddOrder on the desk. Never prints secrets.

Buys are limit IOC at ask + entry.maxSlippageBps and refuse to add to a held
pair. Sells are market so a stop always fills.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import desk_book  # noqa: E402
import desk_halt  # noqa: E402
import desk_rules  # noqa: E402
import kraken_private as kp  # noqa: E402
import kraken_universe as ku  # noqa: E402
from desk_paths import ledger_dir  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text())
DUST_USD = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append(name: str, row: dict) -> None:
    path = ledger_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def paper_mode() -> bool:
    return bool(CONFIG.get("paper"))


def fills_venue() -> str:
    if paper_mode():
        return "paper"
    return (CONFIG.get("venues") or {}).get("fills") or "paper"


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
    for row in desk_book.load_jsonl(ledger_dir() / "fills.jsonl"):
        if row.get("cycle_id") == cycle_id and (row.get("venue") or "paper") == "kraken":
            n += 1
    return n


def fail(payload: dict, code: int) -> int:
    print(json.dumps(payload), file=sys.stderr)
    return code


def cmd_buy(args: argparse.Namespace) -> int:
    require_live()
    if desk_halt.is_halted():
        return fail({"error": "HALTED", "fills": 0}, 2)
    max_per = int((CONFIG.get("live") or {}).get("maxFillsPerCycle") or 1)
    if cycle_fill_count(args.cycle_id) >= max_per:
        return fail({"error": "maxFillsPerCycle", "max": max_per}, 1)
    dollars = float(args.dollars)
    if dollars <= 0:
        return fail({"error": "dollars must be > 0"}, 1)
    block = desk_rules.entry_block(args.pair, desk_book.load_book("kraken"), CONFIG, time.time())
    if block:
        return fail({"error": block, "pair_id": args.pair}, 3)

    meta = ku.pair_index(ku.load_universe()).get(args.pair)
    if not meta:
        return fail({"error": "pair not in universe", "pair_id": args.pair}, 1)
    tick = ku.fetch_tickers([args.pair]).get(args.pair)
    if not tick or not tick.get("ask"):
        return fail({"error": "no ask", "pair_id": args.pair}, 1)
    ask = float(tick["ask"])
    limit = desk_rules.limit_buy_price(ask, CONFIG)
    price = ku.round_price(limit, meta.get("pair_decimals"))
    volume = ku.round_base(dollars / float(price), meta.get("lot_decimals"))
    if float(volume) < float(meta.get("ordermin") or 0):
        return fail({"error": "below ordermin", "volume": volume, "ordermin": meta.get("ordermin")}, 1)

    key, secret = kp.load_keys(CONFIG)
    try:
        oflags = (CONFIG.get("live") or {}).get("buyOflags") or "fciq"
        result = kp.add_order(
            key,
            secret,
            pair=args.pair,
            side="buy",
            volume=volume,
            oflags=oflags,
            ordertype="limit",
            price=price,
            timeinforce="IOC",
            validate=False,
        )
        txids = result.get("txid") or []
        txid = txids[0] if txids else None
        info = wait_order(key, secret, txid) if txid else {}
    finally:
        del key, secret

    vol_exec = float(info.get("vol_exec") or 0)
    if vol_exec <= 0:
        return fail(
            {"unfilled": True, "pair_id": args.pair, "limit_price": price, "ask": ask, "txid": txid,
             "status": info.get("status")},
            4,
        )
    decision = float(args.decision_price or 0) or ask
    fill_px = float(info.get("price") or 0) or float(price)
    cost = float(info.get("cost") or vol_exec * fill_px)
    row = {
        "ts": utc_now(),
        "cycle_id": args.cycle_id,
        "pair_id": args.pair,
        "wsname": args.wsname or meta.get("wsname") or "",
        "requested": round(dollars, 2),
        "filled": round(cost, 2),
        "decision_price": decision,
        "ask": ask,
        "limit_price": float(price),
        "order_type": "limit_ioc",
        "fill_price": fill_px,
        "slippage_bps": int(round((fill_px - decision) / decision * 10000)),
        "fee_percent": float((CONFIG.get("fee") or {}).get("rate") or 0),
        "fee_usd": float(info.get("fee") or 0),
        "partial": vol_exec < float(volume) * 0.999,
        "venue": "kraken",
        "txid": txid,
        "volume_base": vol_exec,
        "descr": (result.get("descr") or {}).get("order"),
    }
    append("fills.jsonl", row)
    print(json.dumps(row, indent=2))
    return 0


def _sell_lock_path() -> Path:
    rd = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(rd) if rd else Path.home() / ".cache"
    return base / "evo-trading-sell.lock"


def managed_position(pair_id: str) -> desk_book.Position | None:
    book = desk_book.load_book("kraken")
    for pos in desk_book.managed_positions(book):
        if pos.pair_id == pair_id:
            return pos
    return None


def cmd_sell(args: argparse.Namespace) -> int:
    require_live()
    lock_path = _sell_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        pos = managed_position(args.pair)
        if pos is None:
            print(json.dumps({"skipped": "already_closed", "pair_id": args.pair}))
            return 0
        return _cmd_sell_locked(args, pos)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _cmd_sell_locked(args: argparse.Namespace, pos: desk_book.Position) -> int:
    meta = ku.pair_index(ku.load_universe()).get(args.pair) or {}
    qty = float(args.volume_base or 0) or pos.qty
    if qty <= 0:
        return fail({"error": "missing volume_base", "pair_id": args.pair}, 1)
    ticks = ku.fetch_tickers([args.pair])
    last = (ticks.get(args.pair) or {}).get("last")
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
    status = str(info.get("status") or "").lower()
    vol_exec = float(info.get("vol_exec") or 0)
    executed = bool(txid) and (vol_exec > 0 or status in {"open", "pending", ""})
    row = {
        "ts": utc_now(),
        "cycle_id": args.cycle_id,
        "pair_id": args.pair,
        "wsname": args.wsname or pos.wsname,
        "action": "CLOSE" if executed else "SELL_FAILED",
        "rule_fired": args.rule or "MANUAL",
        "venue": "kraken",
        "txid": txid,
        "entry": pos.entry,
        "opened_ts": pos.opened_ts,
        "fill_price": float(info.get("price") or 0),
        "filled": float(info.get("cost") or 0),
        "volume_base": vol_exec or float(volume),
        "status": info.get("status"),
    }
    append("closes.jsonl", row)
    print(json.dumps(row, indent=2))
    return 0 if executed else 1


def cmd_opens(_args: argparse.Namespace) -> int:
    venue = fills_venue()
    book = desk_book.load_book(venue)
    opens_out = []
    for pos in book.open_positions():
        row = pos.to_dict()
        row["n"] = pos.n_buys
        opens_out.append(row)
    print(json.dumps({"venue": venue, "paper": paper_mode(), "opens": opens_out}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Kraken execute for the desk")
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
    sell.add_argument("--rule", default="MANUAL")
    sell.add_argument("--volume-base", type=float, default=0)
    sell.set_defaults(func=cmd_sell)

    opens = sub.add_parser("opens")
    opens.set_defaults(func=cmd_opens)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

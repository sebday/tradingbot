"""bin/desk: the Grok desk.

  wake                         hourly decision (Grok 4.7, resumed chat)
  chat                         open that chat in this terminal
  run                          one pass of the exit and entry rules
  snapshot                     print the snapshot JSON
  halt / resume                block new buys; exit rules keep running
  trades [-n N]                recent orders and the reason for each
  trade buy|sell               place an order now
  rule set|list                exit rule on an open position
  entry add|cancel|list        a buy that waits for a price or a time
"""
from __future__ import annotations

import argparse
import json
import sys

from . import rules_lib
from .config import (
    live,
    load_config,
    next_wake_iso,
    read_halt,
    utc_now,
    write_halt,
    clear_halt,
)
from .db import Log, require_reason


def _out(payload, code: int = 0) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return code


def _params(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--params is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("--params must be a JSON object")
    return value


def _services():
    from .live import Services
    return Services(load_config())


def cmd_snapshot(_args) -> int:
    from .snapshot import build
    return _out(build(load_config(), Log(), _services()))


def cmd_run(_args) -> int:
    import fcntl
    import time

    from .config import state_dir
    from .runner import run_once

    lock_path = state_dir() / "runner.lock"
    fd = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return _out({"skipped": "already running"})
    try:
        summary = run_once(Log(), _services(), time.time())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    return _out(summary)


def cmd_wake(_args) -> int:
    import fcntl

    from .config import state_dir
    from .snapshot import build
    from .wake import run_wake

    lock_path = state_dir() / "wake.lock"
    fd = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return _out({"skipped": "already running"})
    try:
        log = Log()
        snapshot = build(load_config(), log, _services())
        (state_dir() / "last-snapshot.json").write_text(json.dumps(snapshot, indent=2, default=str) + "\n", encoding="utf-8")
        result = run_wake(snapshot)
        log.add_wake(utc_now(), result.get("session_id"), "ok" if result.get("ok") else "error", result.get("summary") or result.get("error") or "")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    return _out(result, 0 if result.get("ok") else 1)


def cmd_chat(_args) -> int:
    from .wake import open_chat
    open_chat()
    return 0


def cmd_halt(args) -> int:
    return _out(write_halt(require_reason(args.reason)))


def cmd_resume(_args) -> int:
    was = clear_halt()
    return _out({"resumed": True, "was": was})


def cmd_trades(args) -> int:
    rows = []
    for row in Log().recent_trades(args.n):
        rows.append({k: row[k] for k in ("id", "ts", "side", "pair_id", "wsname", "size_usd", "qty", "price", "order_type", "status", "reason", "rule_name", "txid", "live")})
    return _out(rows)


def cmd_rule_list(_args) -> int:
    rows = []
    for row in Log().active_rules():
        rows.append({
            "pair_id": row["pair_id"], "name": row["name"], "params": json.loads(row["params_json"]),
            "reason": row["reason"], "opened_ts": row["opened_ts"], "set_at": row["set_at"],
        })
    return _out(rows)


def cmd_rule_set(args) -> int:
    params = _params(args.params)
    rules_lib.check_params(args.name, params)
    opened = None
    try:
        for pos in _services().positions():
            if pos.pair_id == args.pair:
                opened = pos.opened_ts
                break
    except Exception as exc:
        print(f"book lookup failed: {exc}", file=sys.stderr)
    rule_id = Log().set_rule({
        "pair_id": args.pair,
        "opened_ts": opened,
        "name": args.name,
        "params": params,
        "source": "custom" if args.name == "custom" else "library",
        "reason": args.reason,
        "set_at": utc_now(),
    })
    return _out({"id": rule_id, "pair_id": args.pair, "name": args.name, "opened_ts": opened})


def _held(services, pair_id: str) -> bool:
    try:
        return any(p.pair_id == pair_id for p in services.positions())
    except Exception:
        return False


def cmd_trade_buy(args) -> int:
    reason = require_reason(args.reason)
    if args.dollars <= 0:
        raise SystemExit("--dollars must be > 0")
    services = _services()
    if services.entries_blocked() or read_halt():
        return _out({"error": "halted"}, 2)
    pair = services.pair(args.pair)
    if not pair:
        return _out({"error": "pair not in universe", "pair_id": args.pair}, 1)
    if _held(services, args.pair):
        return _out({"error": "already held", "pair_id": args.pair}, 1)
    try:
        if services.cash() + 1e-9 < args.dollars:
            return _out({"error": "not enough cash", "pair_id": args.pair}, 1)
    except Exception as exc:
        return _out({"error": f"balance: {exc}"}, 1)
    tick = services.tickers([args.pair]).get(args.pair) or {}
    ask = tick.get("ask") or tick.get("last")
    if not ask:
        return _out({"error": "no ask", "pair_id": args.pair}, 1)
    entry = {"pair_id": args.pair, "wsname": pair.get("wsname"), "size_usd": args.dollars, "reason": reason}
    result = services.buy(entry, float(ask))
    exit_rule = None
    if args.exit_name:
        params = _params(args.exit_params)
        rules_lib.check_params(args.exit_name, params)
        exit_rule = {"name": args.exit_name, "params": params}
        if result.get("ok"):
            Log().set_rule({
                "pair_id": args.pair, "opened_ts": result.get("opened_ts"), "name": args.exit_name,
                "params": params, "source": "custom" if args.exit_name == "custom" else "library",
                "reason": reason, "set_at": utc_now(),
            })
    trade_id = Log().add_trade({
        "ts": utc_now(), "side": "buy", "pair_id": args.pair, "wsname": pair.get("wsname"),
        "size_usd": result.get("cost_usd") or args.dollars, "qty": result.get("qty"), "price": result.get("price"),
        "order_type": "limit_ioc", "reason": reason, "rule_name": args.exit_name,
        "rule_params": json.dumps(exit_rule["params"]) if exit_rule else None,
        "txid": result.get("txid"), "status": result.get("status") or ("filled" if result.get("ok") else "failed"),
        "live": 1 if result.get("live") else 0,
        "raw_json": json.dumps({"error": result.get("error"), "live_mode": live()}),
    })
    return _out({"id": trade_id, **{k: result.get(k) for k in ("ok", "status", "error", "qty", "price", "txid")}}, 0 if result.get("ok") else 1)


def cmd_trade_sell(args) -> int:
    reason = require_reason(args.reason)
    services = _services()
    pos = next((p for p in services.positions() if p.pair_id == args.pair), None)
    if pos is None:
        return _out({"error": "not held", "pair_id": args.pair}, 1)
    result = services.sell(pos, reason, "manual")
    if result.get("ok"):
        for rule in Log().active_rules():
            if rule["pair_id"] == args.pair:
                Log().deactivate_rule(rule["id"])
    trade_id = Log().add_trade({
        "ts": utc_now(), "side": "sell", "pair_id": args.pair, "wsname": pos.wsname,
        "qty": result.get("qty") or pos.qty, "price": result.get("price"), "size_usd": result.get("proceeds_usd"),
        "order_type": "market", "reason": reason, "rule_name": "manual", "txid": result.get("txid"),
        "status": result.get("status") or ("filled" if result.get("ok") else "failed"),
        "live": 1 if result.get("live") else 0,
        "raw_json": json.dumps({"error": result.get("error")}),
    })
    return _out({"id": trade_id, "ok": result.get("ok"), "status": result.get("status"), "error": result.get("error")}, 0 if result.get("ok") else 1)


def cmd_entry_list(_args) -> int:
    rows = []
    for row in Log().waiting_entries():
        rows.append({
            "id": row["id"], "pair_id": row["pair_id"], "kind": row["kind"], "size_usd": row["size_usd"],
            "params": json.loads(row["params_json"]), "exit_rule": json.loads(row["exit_rule_json"]),
            "reason": row["reason"], "expires_at": row["expires_at"], "status": row["status"],
            "kraken_txid": row["kraken_txid"], "last_error": row["last_error"],
        })
    return _out(rows)


def cmd_entry_add(args) -> int:
    reason = require_reason(args.reason)
    if args.dollars <= 0:
        raise SystemExit("--dollars must be > 0")
    params: dict = {}
    if args.price is not None:
        params["price"] = args.price
    if args.at:
        params["at"] = args.at
    if args.band_low is not None:
        params["band_low"] = args.band_low
    if args.band_high is not None:
        params["band_high"] = args.band_high
    if args.module:
        params["module"] = args.module
    params.update(_params(args.params))
    rules_lib.check_entry(args.kind, params)
    exit_params = _params(args.exit_params)
    rules_lib.check_params(args.exit_name, exit_params)
    services = _services()
    if services.entries_blocked() or read_halt():
        return _out({"error": "halted"}, 2)
    pair = services.pair(args.pair)
    if not pair:
        return _out({"error": "pair not in universe", "pair_id": args.pair}, 1)
    if _held(services, args.pair):
        return _out({"error": "already held", "pair_id": args.pair}, 1)
    entry_id = Log().add_entry({
        "ts": utc_now(), "pair_id": args.pair, "wsname": pair.get("wsname"), "size_usd": args.dollars,
        "kind": args.kind, "params": params,
        "exit_rule": {"name": args.exit_name, "params": exit_params, "reason": reason},
        "reason": reason, "expires_at": args.expires or next_wake_iso(), "status": "waiting", "live": live(),
    })
    return _out({"id": entry_id, "pair_id": args.pair, "kind": args.kind, "expires_at": args.expires or next_wake_iso(), "live": live()})


def cmd_entry_cancel(args) -> int:
    reason = require_reason(args.reason)
    log = Log()
    row = next((e for e in log.waiting_entries() if e["id"] == args.id), None)
    if row is None:
        return _out({"error": "no waiting entry", "id": args.id}, 1)
    if row.get("kraken_txid"):
        _services().cancel(row["kraken_txid"])
    log.update_entry(args.id, status="cancelled", last_error=reason)
    return _out({"id": args.id, "status": "cancelled"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk", description="Grok desk")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("wake").set_defaults(func=cmd_wake)
    sub.add_parser("chat").set_defaults(func=cmd_chat)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("snapshot").set_defaults(func=cmd_snapshot)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    h = sub.add_parser("halt")
    h.add_argument("--reason", required=True)
    h.set_defaults(func=cmd_halt)
    t = sub.add_parser("trades")
    t.add_argument("-n", type=int, default=20)
    t.set_defaults(func=cmd_trades)

    rule = sub.add_parser("rule")
    rule_sub = rule.add_subparsers(dest="rule_cmd", required=True)
    rule_sub.add_parser("list").set_defaults(func=cmd_rule_list)
    rs = rule_sub.add_parser("set")
    rs.add_argument("--pair", required=True)
    rs.add_argument("--name", required=True)
    rs.add_argument("--params", default="")
    rs.add_argument("--reason", required=True)
    rs.set_defaults(func=cmd_rule_set)

    trade = sub.add_parser("trade")
    trade_sub = trade.add_subparsers(dest="trade_cmd", required=True)
    buy = trade_sub.add_parser("buy")
    buy.add_argument("--pair", required=True)
    buy.add_argument("--dollars", required=True, type=float)
    buy.add_argument("--reason", required=True)
    buy.add_argument("--exit-name", default="")
    buy.add_argument("--exit-params", default="")
    buy.set_defaults(func=cmd_trade_buy)
    sell = trade_sub.add_parser("sell")
    sell.add_argument("--pair", required=True)
    sell.add_argument("--reason", required=True)
    sell.set_defaults(func=cmd_trade_sell)

    entry = sub.add_parser("entry")
    entry_sub = entry.add_subparsers(dest="entry_cmd", required=True)
    entry_sub.add_parser("list").set_defaults(func=cmd_entry_list)
    add = entry_sub.add_parser("add")
    add.add_argument("--pair", required=True)
    add.add_argument("--dollars", required=True, type=float)
    add.add_argument("--kind", required=True, choices=("buy_limit", "buy_stop", "at_time", "custom"))
    add.add_argument("--price", type=float)
    add.add_argument("--at")
    add.add_argument("--expires")
    add.add_argument("--band-low", type=float)
    add.add_argument("--band-high", type=float)
    add.add_argument("--module")
    add.add_argument("--params", default="")
    add.add_argument("--exit-name", required=True)
    add.add_argument("--exit-params", default="")
    add.add_argument("--reason", required=True)
    add.set_defaults(func=cmd_entry_add)
    cancel = entry_sub.add_parser("cancel")
    cancel.add_argument("--id", required=True, type=int)
    cancel.add_argument("--reason", required=True)
    cancel.set_defaults(func=cmd_entry_cancel)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""One pass over exit rules and waiting buys. Sells or buys only when a rule says so.

A position with no active rule gets the risk.stopLossPct stop and the risk.trailArmPct /
risk.trailGiveback trail. This runner is the only seller.
"""
from __future__ import annotations

import json

from .config import iso, parse_ts, utc_now
from .live import DUST_USD
from .rules_lib import as_dict, decide_entry, decide_exit


def _scripts():
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import desk_rules
    import kraken_universe as ku

    return desk_rules, ku


def position_ctx(pos, tick: dict, ohlc: list, now: float) -> dict:
    rules, ku = _scripts()
    peak, known = rules.peak_since_fill(ohlc, rules.parse_ts(getattr(pos, "opened_ts", None)))
    stats = ku.volume_ratio_6h_24h(ohlc) if ohlc else None
    return {
        "last": tick.get("last"),
        "bid": tick.get("bid"),
        "ask": tick.get("ask"),
        "entry": getattr(pos, "entry", None),
        "peak": peak if known else None,
        "volume_ratio": None if not stats else stats.get("ratio"),
        "now": now,
        "opened_ts": getattr(pos, "opened_ts", None),
        "qty": getattr(pos, "qty", None),
    }


def default_exit(services) -> dict:
    """The stop and trail a position gets while Grok has set no rule on it."""
    risk = (getattr(services, "cfg", None) or {}).get("risk") or {}
    return {
        "pct": float(risk.get("stopLossPct", 0.08)),
        "arm_pct": float(risk.get("trailArmPct", 0.08)),
        "giveback": float(risk.get("trailGiveback", 0.05)),
    }


def _sell(log, services, pos, reason: str, rule_name: str, rule_params: str) -> dict:
    result = services.sell(pos, reason, rule_name)
    if result.get("status") == "already_closed":
        return result
    log.add_trade({
        "ts": utc_now(),
        "side": "sell",
        "pair_id": pos.pair_id,
        "wsname": pos.wsname,
        "qty": result.get("qty") or pos.qty,
        "price": result.get("price"),
        "size_usd": result.get("proceeds_usd"),
        "order_type": "market",
        "reason": reason,
        "rule_name": rule_name,
        "rule_params": rule_params,
        "txid": result.get("txid"),
        "status": result.get("status") or ("filled" if result.get("ok") else "failed"),
        "live": 1 if result.get("live") else 0,
        "raw_json": json.dumps({k: result.get(k) for k in ("ok", "error", "status")}, default=str),
    })
    return result


def run_once(log, services, now: float) -> dict:
    """Returns a summary. A failed rule holds the position; it never sells on an error."""
    positions = {p.pair_id: p for p in services.positions()}
    rules = log.active_rules()
    waiting = log.waiting_entries()
    want = sorted({r["pair_id"] for r in rules} | {e["pair_id"] for e in waiting} | set(positions))
    ticks = services.tickers(want) if want else {}
    ohlc_cache: dict[str, list] = {}

    def candles(pair_id: str) -> list:
        if pair_id not in ohlc_cache:
            try:
                ohlc_cache[pair_id] = services.ohlc(pair_id) or []
            except Exception:
                ohlc_cache[pair_id] = []
        return ohlc_cache[pair_id]

    closed = []
    bought = []
    errors = []
    ruled = set()

    for rule in rules:
        pos = positions.get(rule["pair_id"])
        if pos is None:
            if rule.get("opened_ts"):
                log.deactivate_rule(rule["id"])
            continue
        if rule.get("opened_ts") and rule["opened_ts"] != pos.opened_ts:
            log.deactivate_rule(rule["id"])
            continue
        if not rule.get("opened_ts") and pos.opened_ts:
            log.bind_rule(rule["id"], pos.opened_ts)
        ruled.add(pos.pair_id)
        ctx = position_ctx(pos, ticks.get(rule["pair_id"]) or {}, candles(rule["pair_id"]), now)
        params = as_dict(rule["params_json"])
        word = decide_exit(rule["name"], params, ctx)
        if word is None:
            errors.append({"pair_id": rule["pair_id"], "error": "rule failed; held"})
            continue
        if word != "sell":
            continue
        result = _sell(log, services, pos, rule["reason"], rule["name"], rule["params_json"])
        if result.get("status") == "already_closed" or result.get("ok"):
            log.deactivate_rule(rule["id"])
        if result.get("ok") and result.get("status") != "already_closed":
            closed.append(pos.pair_id)
        elif not result.get("ok"):
            errors.append({"pair_id": pos.pair_id, "error": result.get("error") or "sell failed"})

    default = default_exit(services)
    for pid, pos in positions.items():
        if pid in ruled:
            continue
        last = (ticks.get(pid) or {}).get("last")
        if last is None or float(last) * float(pos.qty or 0) < DUST_USD:
            continue
        ctx = position_ctx(pos, ticks.get(pid) or {}, candles(pid), now)
        if decide_exit("stop_pct", default, ctx) == "sell":
            rule_name, reason = "stop_pct", f"Default {default['pct']:.0%} stop; no rule set."
        elif decide_exit("trail", default, ctx) == "sell":
            rule_name = "trail"
            reason = f"Default trail: {default['giveback']:.0%} off the high after {default['arm_pct']:.0%} up; no rule set."
        else:
            continue
        result = _sell(log, services, pos, reason, rule_name, json.dumps(default))
        if result.get("ok") and result.get("status") != "already_closed":
            closed.append(pos.pair_id)
        elif not result.get("ok"):
            errors.append({"pair_id": pos.pair_id, "error": result.get("error") or "sell failed"})

    if services.entries_blocked():
        for entry in waiting:
            if entry.get("kraken_txid"):
                services.cancel(entry["kraken_txid"])
            log.update_entry(entry["id"], status="cancelled", last_error="halted")
        return {"ts": iso(now), "closed": closed, "bought": bought, "errors": errors, "halted": True}

    for entry in waiting:
        outcome = _entry(log, services, entry, positions, ticks, now)
        if outcome.get("bought"):
            bought.append(outcome["pair_id"])
        if outcome.get("error"):
            errors.append({"pair_id": entry["pair_id"], "error": outcome["error"]})

    return {"ts": iso(now), "closed": closed, "bought": bought, "errors": errors, "halted": False}


def _entry(log, services, entry: dict, positions: dict, ticks: dict, now: float) -> dict:
    expires = parse_ts(entry.get("expires_at"))
    if expires is not None and now >= expires:
        if entry.get("kraken_txid"):
            services.cancel(entry["kraken_txid"])
        log.update_entry(entry["id"], status="expired")
        return {}
    if entry["pair_id"] in positions:
        if entry.get("kraken_txid"):
            services.cancel(entry["kraken_txid"])
        log.update_entry(entry["id"], status="cancelled", last_error="already held")
        return {}

    params = as_dict(entry["params_json"])
    kind = entry["kind"]
    tick = ticks.get(entry["pair_id"]) or {}

    if kind in {"buy_limit", "buy_stop"} and services.live:
        return _resting(log, services, entry, params, now)

    ctx = {"last": tick.get("last"), "bid": tick.get("bid"), "ask": tick.get("ask"), "now": now}
    word = decide_entry(kind, params, ctx)
    if word is None:
        log.update_entry(entry["id"], last_error="condition failed; still waiting")
        return {"error": "condition failed; still waiting"}
    if word != "buy":
        return {}
    return _send_buy(log, services, entry, params, tick, now, order_type=kind)


def _resting(log, services, entry: dict, params: dict, now: float) -> dict:
    txid = entry.get("kraken_txid")
    if not txid:
        placed = services.place_resting(entry)
        if not placed.get("ok"):
            log.update_entry(entry["id"], last_error=str(placed.get("error") or "place failed")[:300])
            return {"error": placed.get("error") or "place failed"}
        log.update_entry(entry["id"], kraken_txid=placed.get("txid"), last_error=None)
        txid = placed.get("txid")
        if not txid:
            return {}
    info = services.query(txid)
    status = str(info.get("status") or "").lower()
    vol = float(info.get("vol_exec") or 0)
    if status in {"canceled", "cancelled", "expired"} and vol <= 0:
        log.update_entry(entry["id"], status="expired" if status == "expired" else "cancelled")
        return {}
    if vol <= 0:
        return {}
    if status not in {"closed", "canceled", "cancelled", "expired"}:
        return {}
    fill = {
        "ok": True,
        "live": True,
        "status": "filled",
        "qty": vol,
        "price": float(info.get("price") or params.get("price") or 0),
        "proceeds_usd": float(info.get("cost") or 0),
        "txid": txid,
        "opened_ts": utc_now(),
        "wsname": entry.get("wsname") or "",
    }
    services.note_fill(entry, fill)
    return _record_buy(log, entry, fill, order_type=entry["kind"])


def _send_buy(log, services, entry: dict, params: dict, tick: dict, now: float, order_type: str) -> dict:
    price = tick.get("ask") or tick.get("last") or params.get("price")
    if not price:
        log.update_entry(entry["id"], last_error="no price")
        return {"error": "no price"}
    if services.cash() + 1e-9 < float(entry["size_usd"]):
        log.update_entry(entry["id"], last_error="not enough cash")
        return {"error": "not enough cash"}
    result = services.buy(entry, float(price))
    if not result.get("ok"):
        log.update_entry(entry["id"], last_error=str(result.get("error") or "buy failed")[:300])
        return {"error": result.get("error") or "buy failed"}
    return _record_buy(log, entry, result, order_type=order_type)


def _record_buy(log, entry: dict, result: dict, order_type: str) -> dict:
    log.add_trade({
        "ts": utc_now(),
        "side": "buy",
        "pair_id": entry["pair_id"],
        "wsname": result.get("wsname") or entry.get("wsname"),
        "size_usd": result.get("cost_usd") or entry["size_usd"],
        "qty": result.get("qty"),
        "price": result.get("price"),
        "order_type": order_type,
        "reason": entry["reason"],
        "rule_name": order_type,
        "txid": result.get("txid"),
        "status": result.get("status") or "filled",
        "live": 1 if result.get("live") else 0,
        "entry_id": entry["id"],
        "raw_json": json.dumps({"ok": result.get("ok"), "error": result.get("error")}, default=str),
    })
    exit_rule = as_dict(entry["exit_rule_json"])
    if exit_rule.get("name"):
        log.set_rule({
            "pair_id": entry["pair_id"],
            "opened_ts": result.get("opened_ts"),
            "name": exit_rule["name"],
            "params": exit_rule.get("params") or {},
            "source": "custom" if exit_rule["name"] == "custom" else "library",
            "reason": exit_rule.get("reason") or entry["reason"],
            "set_at": utc_now(),
        })
    log.update_entry(entry["id"], status="filled", last_error=None)
    return {"bought": True, "pair_id": entry["pair_id"]}

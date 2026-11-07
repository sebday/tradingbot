"""Executor: the hands. Acts only on gate-approved decisions and journals every order.

The executor re-runs the gate with fresh prices immediately before each order, so a
decision approved minutes earlier cannot trade at a stale price.
"""
from __future__ import annotations

from . import policy
from .book import load_book
from .util import utc_now


def execute(cfg: dict, journal, broker, market, mandate, decisions: list[dict], gate_state) -> list[dict]:
    results = []
    if broker.venue != mandate.venue:
        raise RuntimeError(f"broker venue {broker.venue} != mandate venue {mandate.venue}")
    if broker.venue == "kraken" and (cfg.get("harness") or {}).get("mode") != "live":
        raise RuntimeError("refusing kraken orders: harness.mode is not live")
    universe = {r["pair_id"]: r for r in mandate.universe}
    for d in decisions:
        if not d.get("approved") or d["action"] == "hold":
            continue
        pid = d["pair_id"]
        pair = universe.get(pid) or {"pair_id": pid}
        if d["action"] == "tighten_stop":
            book = load_book(journal, mandate.venue)
            pos = book.position(pid)
            if pos and (pos.stop_price is None or d["stop_price"] > pos.stop_price):
                journal.set_stop(mandate.venue, pid, pos.opened_ts, d["stop_price"], "tighten_stop", mandate.cycle_id)
                results.append({"pair_id": pid, "action": "tighten_stop", "stop_price": d["stop_price"], "ok": True})
            continue
        if d["action"] == "open":
            fresh = market.tickers([pid]).get(pid) or {}
            if not fresh.get("ask"):
                results.append({"pair_id": pid, "action": "open", "ok": False, "error": "NO_PRICE"})
                continue
            # Recheck the stop band and limit against the fresh ask; the stop is the proposal's.
            lo, hi = policy.stop_band(float(fresh["ask"]), cfg)
            if not (lo - 1e-12 <= d["stop_price"] <= hi + 1e-12):
                results.append({"pair_id": pid, "action": "open", "ok": False, "error": "STOP_BAND_MOVED",
                                "ask": fresh["ask"]})
                continue
            limit = policy.limit_buy_price(float(fresh["ask"]), cfg)
            res = broker.buy_limit_ioc(pair, d["size_usd"], limit, mandate.cycle_id)
            order = res.get("order") or {}
            oid = journal.add_order({"cycle_id": mandate.cycle_id, "venue": broker.venue, "pair_id": pid, "side": "buy",
                                     "ordertype": order.get("ordertype"), "price": order.get("price"),
                                     "volume": order.get("volume"), "oflags": order.get("oflags"),
                                     "timeinforce": order.get("timeinforce"), "status": res.get("status"),
                                     "txid": res.get("txid"), "proposal_id": d.get("proposal_id"), "source": "cycle",
                                     "raw": res})
            if res.get("status") == "filled":
                ts = utc_now()
                journal.add_fill({"ts": ts, "cycle_id": mandate.cycle_id, "venue": broker.venue, "pair_id": pid,
                                  "wsname": pair.get("wsname"), "qty": res["qty"], "cost_usd": res["cost_usd"],
                                  "price": res.get("price"), "fee_usd": res.get("fee_usd"), "txid": res.get("txid"),
                                  "order_id": oid, "source": "cycle", "dedupe": f"fill:{broker.venue}:{res.get('txid')}",
                                  "raw": res})
                book = load_book(journal, mandate.venue)
                pos = book.position(pid)
                journal.set_stop(mandate.venue, pid, pos.opened_ts if pos else ts, d["stop_price"], "proposal",
                                 mandate.cycle_id)
            results.append({"pair_id": pid, "action": "open", "ok": res.get("status") == "filled", "order_id": oid,
                            "status": res.get("status"), "qty": res.get("qty"), "cost_usd": res.get("cost_usd"),
                            "price": res.get("price")})
            continue
        if d["action"] == "close":
            results.append(close_position(cfg, journal, broker, pair, mandate.venue, mandate.cycle_id, "SEAT_CLOSE"))
    return results


def close_position(cfg: dict, journal, broker, pair: dict, venue: str, cycle_id: str, rule: str,
                   source: str = "cycle") -> dict:
    pid = pair["pair_id"]
    book = load_book(journal, venue)
    pos = book.position(pid)
    if pos is None:
        return {"pair_id": pid, "action": "close", "ok": True, "skipped": "already_closed"}
    pair = {**pair, "wsname": pair.get("wsname") or pos.wsname}
    res = broker.sell_market(pair, pos.qty, cycle_id)
    order = res.get("order") or {}
    oid = journal.add_order({"cycle_id": cycle_id, "venue": broker.venue, "pair_id": pid, "side": "sell",
                             "ordertype": "market", "volume": order.get("volume"), "oflags": order.get("oflags"),
                             "status": res.get("status"), "txid": res.get("txid"), "source": source, "raw": res})
    if res.get("status") == "closed":
        journal.add_close({"ts": utc_now(), "cycle_id": cycle_id, "venue": broker.venue, "pair_id": pid,
                           "wsname": pair.get("wsname"), "qty": res.get("qty"), "proceeds_usd": res.get("proceeds_usd"),
                           "price": res.get("price"), "fee_usd": res.get("fee_usd"), "rule_fired": rule,
                           "txid": res.get("txid"), "status": "closed", "order_id": oid, "source": source,
                           "dedupe": f"close:{broker.venue}:{res.get('txid')}", "raw": res})
    return {"pair_id": pid, "action": "close", "rule": rule, "ok": res.get("status") == "closed", "order_id": oid,
            "status": res.get("status"), "proceeds_usd": res.get("proceeds_usd")}

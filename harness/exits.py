"""Mechanical exits, every 5 minutes, independent of the model.

Uses each position's stored stop_price (frozen at import for legacy positions, proposal stop
for harness entries, raised only by tighten_stop). Ported from scripts/desk_exit_check.py.

Venues:
  paper  - harness paper book; sells through PaperBroker.
  kraken - SHADOW by default: decisions are journaled and compared with the legacy rule, no
           sells (the legacy evo-trading-exit.timer still owns live exits). Set
           harness.exits.liveKraken=true at cutover to let this path sell.
"""
from __future__ import annotations

import json
import time

from . import config as config_mod, policy
from .book import load_book
from .executor import close_position
from .market.kraken_public import volume_ratio_6h_24h
from .util import parse_ts, utc_now

DUST_USD = 1.0


def _retry(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            value = fn()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value
        if i + 1 < attempts:
            time.sleep(0.4 * (i + 1))
    return None


def measure(market, pair_id: str, opened_ts: str | None) -> dict:
    tick = _retry(lambda: market.tickers([pair_id]).get(pair_id))
    ohlc = _retry(lambda: market.ohlc(pair_id, 60)) or []
    stats = volume_ratio_6h_24h(ohlc) if ohlc else None
    peak, known = policy.peak_since_fill(ohlc, parse_ts(opened_ts))
    last = (tick or {}).get("last")
    return {"last": float(last) if last is not None else None, "bid": (tick or {}).get("bid"),
            "ratio": None if not stats else stats.get("ratio"),
            "volume_known": stats is not None and stats.get("ratio") is not None, "peak": peak, "peak_known": known}


def run_exits(cfg: dict, journal, market, broker, venue: str, do_sell: bool, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    cycle_id = "exits-" + utc_now()
    journal.run_start(cycle_id, "exits", config_mod.mode(cfg))
    book = load_book(journal, venue)
    results = []
    universe = {}
    try:
        universe = {p["pair_id"]: p for p in (market.universe().get("pairs") or [])}
    except Exception:  # noqa: BLE001
        universe = {}
    for pos in book.open_positions():
        if pos.qty <= 0:
            continue
        m = measure(market, pos.pair_id, pos.opened_ts)
        last = m["last"]
        if last is not None and pos.qty * last < DUST_USD:
            results.append({"pair_id": pos.pair_id, "action": "SKIP_DUST", "last": last})
            continue
        decision = policy.decide(pos.entry, pos.stop_price, m["peak"], m["peak_known"], last, m["ratio"],
                                 m["volume_known"], cfg)
        legacy_decision = policy.decide(pos.entry, policy.legacy_stop_price(pos.entry, cfg), m["peak"], m["peak_known"],
                                        last, m["ratio"], m["volume_known"], cfg)
        closing = decision in policy.CLOSE_RULES
        row = {"pair_id": pos.pair_id, "wsname": pos.wsname, "action": "CLOSE" if closing else decision,
               "rule_fired": decision if closing else None, "entry": pos.entry, "stop": pos.stop_price,
               "stop_source": pos.stop_source, "last": last,
               "pct_vs_entry": (last / pos.entry - 1.0) if last and pos.entry else None, "peak": m["peak"],
               "trail_armed": policy.trail_armed(pos.entry, m["peak"], m["peak_known"], cfg), "ratio": m["ratio"],
               "legacy_decision": legacy_decision}
        if closing and do_sell:
            pair = universe.get(pos.pair_id) or {"pair_id": pos.pair_id}
            row["sell"] = close_position(cfg, journal, broker, pair, venue, cycle_id, decision, source="exits")
        results.append(row)
    summary = {"cycle_id": cycle_id, "kind": "exits", "venue": venue, "do_sell": do_sell, "ts": utc_now(),
               "checked": len(results), "closes": [r for r in results if r.get("action") == "CLOSE"],
               "stale": [r["pair_id"] for r in results if r.get("action") == policy.STALE],
               "legacy_disagreements": [r["pair_id"] for r in results
                                        if r.get("legacy_decision") and r.get("legacy_decision") != (r.get("rule_fired") or r.get("action"))],
               "results": results}
    if summary["legacy_disagreements"]:
        journal.add_incident("EXIT_LEGACY_DISAGREE", json.dumps(summary["legacy_disagreements"]), "exits_shadow",
                             "test_exits_match_legacy_rule_for_imported_stops", cycle_id)
    journal.event("exits", cycle_id, {k: v for k, v in summary.items() if k != "results"})
    journal.run_finish(cycle_id, "ok", None, {k: summary[k] for k in ("venue", "do_sell", "checked", "stale",
                                                                    "legacy_disagreements")}
                       | {"closes": [c["pair_id"] for c in summary["closes"]]})
    return summary

"""Build policy.GateState from a mandate + journal. No keys, no broker (the seat server uses it)."""
from __future__ import annotations

import time

from . import policy
from .book import load_book, stops_map


def build(cfg: dict, journal, mandate, tickers: dict | None = None, now_epoch: float | None = None,
          state_counters: dict | None = None) -> policy.GateState:
    book = load_book(journal, mandate.venue)
    universe = {r["pair_id"]: r for r in mandate.universe}
    ticks = {pid: {"ask": r.get("ask"), "bid": r.get("bid"), "last": r.get("last")} for pid, r in universe.items()}
    for p in (mandate.portfolio or {}).get("positions") or []:
        if p.get("last") is not None:
            ticks.setdefault(p["pair_id"], {"ask": p.get("ask") or p["last"], "bid": p.get("bid") or p["last"],
                                            "last": p["last"]})
    if tickers:
        for pid, t in tickers.items():
            ticks[pid] = {"ask": t.get("ask"), "bid": t.get("bid"), "last": t.get("last")}
    port = mandate.portfolio or {}
    st = policy.GateState(
        cfg=cfg, book=book, equity_usd=float(port.get("equity_usd") or 0), usd_spot=float(port.get("usd_spot") or 0),
        now_epoch=time.time() if now_epoch is None else now_epoch, halted=bool(port.get("halted")),
        allowed_actions=list(mandate.allowed_actions), universe=universe, tickers=ticks, stops=stops_map(book),
        budget_usd=float(mandate.budget_usd), max_fills=int(mandate.max_fills),
        partial=bool(port.get("context_degraded")), invested_usd=port.get("invested_usd"))
    for k, v in (state_counters or {}).items():
        setattr(st, k, v)
    return st


def snapshot(state: policy.GateState) -> dict:
    """Plain-data gate inputs for replay."""
    b = state.book
    return {
        "equity_usd": state.equity_usd, "usd_spot": state.usd_spot, "now_epoch": state.now_epoch,
        "halted": state.halted, "allowed_actions": state.allowed_actions, "universe": state.universe,
        "tickers": state.tickers, "stops": state.stops, "budget_usd": state.budget_usd, "max_fills": state.max_fills,
        "partial": state.partial, "invested_usd": state.invested_usd,
        "book": {"venue": b.venue, "positions": {k: v.to_dict() for k, v in b.positions.items()},
                 "closed": [{"pair_id": c.pair_id, "opened_ts": c.opened_ts, "closed_ts": c.closed_ts} for c in b.closed],
                 "last_close_ts": b.last_close_ts},
    }


def from_snapshot(cfg: dict, snap: dict) -> policy.GateState:
    from .book import Book, ClosedEpisode, Position
    bk = snap["book"]
    positions = {}
    for k, v in bk["positions"].items():
        positions[k] = Position(pair_id=v["pair_id"], wsname=v.get("wsname") or "", venue=v.get("venue") or bk["venue"],
                                qty=v["qty"], cost_usd=v["cost_usd"], opened_ts=v.get("opened_ts"),
                                stop_price=v.get("stop_price"))
    closed = [ClosedEpisode(pair_id=c["pair_id"], wsname="", opened_ts=c.get("opened_ts"), closed_ts=c["closed_ts"],
                            cost_usd=0, proceeds_usd=0, n_buys=0, rule_fired=None) for c in bk["closed"]]
    book = Book(venue=bk["venue"], positions=positions, closed=closed, last_close_ts=bk["last_close_ts"])
    return policy.GateState(cfg=cfg, book=book, equity_usd=snap["equity_usd"], usd_spot=snap["usd_spot"],
                            now_epoch=snap["now_epoch"], halted=snap["halted"], allowed_actions=snap["allowed_actions"],
                            universe=snap["universe"], tickers=snap["tickers"], stops=snap["stops"],
                            budget_usd=snap["budget_usd"], max_fills=snap["max_fills"], partial=snap["partial"],
                            invested_usd=snap.get("invested_usd"))

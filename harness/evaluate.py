"""Daily evaluator (no model). Grades each final proposal and its gate decision once 24h
have passed: forward return at 1h/6h/24h from the decision-hour close, max adverse excursion
over 24h, stop hit, vs BTC hold and the mechanical baseline (rank-1 pair of the mandate
universe, same rank as the legacy shortlist)."""
from __future__ import annotations

import json
import time

from . import paths
from .util import parse_ts, read_json, utc_now

BTC = "XXBTZUSD"


def _bar_index(ohlc: list, epoch: float) -> int | None:
    hour = int(epoch) - int(epoch) % 3600
    for i, c in enumerate(ohlc):
        if int(float(c[0])) == hour:
            return i
    return None


def forward(ohlc: list, epoch: float, stop: float | None = None) -> dict | None:
    i = _bar_index(ohlc, epoch)
    if i is None or i + 24 >= len(ohlc):
        return None
    p0 = float(ohlc[i][4])
    if p0 <= 0:
        return None

    def ret(h):
        return float(ohlc[i + h][4]) / p0 - 1.0
    window = ohlc[i + 1:i + 25]
    low = min(float(c[3]) for c in window)
    return {"p0": p0, "ret_1h": ret(1), "ret_6h": ret(6), "ret_24h": ret(24), "mae_24h": low / p0 - 1.0,
            "stop_hit": None if stop is None else int(low <= stop)}


def run_eval(cfg: dict, journal, market, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    rows = journal.q(
        "SELECT p.*, g.approved AS g_approved, g.id AS gate_id FROM proposals p "
        "LEFT JOIN gate_decisions g ON g.proposal_id = p.id AND g.dry_run = 0 "
        "WHERE p.final = 1 AND p.action != 'hold' AND p.id NOT IN (SELECT subject_id FROM grades WHERE subject='proposal')")
    cache: dict = {}

    def bars(pid):
        if pid not in cache:
            try:
                cache[pid] = market.ohlc(pid, 60)
            except Exception:  # noqa: BLE001
                cache[pid] = []
        return cache[pid]

    graded, pending = 0, 0
    for r in rows:
        ts = parse_ts(r["ts"])
        if ts is None or now - ts < 25 * 3600:
            pending += 1
            continue
        fw = forward(bars(r["pair_id"]), ts, r["stop_price"])
        if fw is None:
            pending += 1
            continue
        btc = forward(bars(BTC), ts)
        receipt = read_json(paths.runs_dir() / r["cycle_id"].replace(":", "") / "receipt.json", {}) or {}
        base_pair = receipt.get("baseline_pair")
        base = forward(bars(base_pair), ts) if base_pair else None
        journal.x(
            "INSERT OR IGNORE INTO grades(ts,subject,subject_id,cycle_id,pair_id,action,approved,p0,ret_1h,ret_6h,ret_24h,"
            "mae_24h,stop_hit,btc_24h,baseline_pair,baseline_24h,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utc_now(), "proposal", r["id"], r["cycle_id"], r["pair_id"], r["action"], r["g_approved"], fw["p0"],
             fw["ret_1h"], fw["ret_6h"], fw["ret_24h"], fw["mae_24h"], fw["stop_hit"],
             (btc or {}).get("ret_24h"), base_pair, (base or {}).get("ret_24h"), json.dumps({"gate_id": r["gate_id"]})))
        graded += 1
    summary = {"ts": utc_now(), "graded": graded, "pending": pending}
    g = journal.q("SELECT action, approved, COUNT(*) n, AVG(ret_24h) r24, AVG(btc_24h) b24, AVG(baseline_24h) base24, "
                  "AVG(stop_hit) sh FROM grades GROUP BY action, approved")
    summary["by_action"] = [dict(x) for x in g]
    journal.event("eval", None, summary)
    return summary

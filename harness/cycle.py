"""One desk cycle: sensors -> mandate -> seat -> gate -> executor -> receipt.

Any failure defaults to no trade. Every run leaves runs/<cycle_id>/receipt.json.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import HARNESS_VERSION, config as config_mod, gatestate, paths, policy, sensors
from .book import load_book
from .contract import Mandate, universe_hash
from .executor import execute
from .util import iso, sha256_obj, utc_now, write_json


@contextmanager
def cycle_lock(name: str = "desk-cycle.lock"):
    p = paths.state_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = p.open("a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        raise RuntimeError(f"{name} held: another run is in progress")
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(paths.ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        sha = out.stdout.strip() or None
        dirty = subprocess.run(["git", "-C", str(paths.ROOT), "status", "--porcelain"], capture_output=True, text=True,
                               timeout=5).stdout.strip()
        return f"{sha}{'+dirty' if dirty else ''}" if sha else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def new_cycle_id(now: float) -> str:
    return iso(now)


def build_mandate(cfg: dict, journal, venue: str, facts: dict, observe: list[str], now: float, cycle_id: str) -> Mandate:
    h = cfg["harness"]
    book = load_book(journal, venue)
    bal = facts.get("balance") or {}
    equity = float(bal.get("equity_usd") or 0)
    usd_spot = float(bal.get("usd_spot") or 0)
    pairs = (facts.get("universe") or {}).get("pairs") or []
    ranked = policy.rank_shortlist(pairs, facts.get("tickers") or {}, book, cfg, now)
    universe = ranked["candidates"]
    halted = sensors.is_halted(journal) is not None
    invested = max(0.0, equity - usd_spot)
    partial = bool(facts.get("context_degraded"))
    cap, _ = policy.ticket_usd(equity, usd_spot, invested, 0.0, cfg, partial=partial)
    room = max(0.0, equity * float(cfg["entry"].get("maxExposurePct", 0.5)) - invested)
    budget = round(min(room, usd_spot * (1 - float(cfg["fee"].get("rate") or 0.004))), 2) if equity else 0.0
    if observe:
        allowed = ["hold"]
    elif halted:
        allowed = ["close", "tighten_stop", "hold"]
    else:
        allowed = ["open", "close", "tighten_stop", "hold"]
    marks = facts.get("tickers") or {}
    positions = []
    for p in book.open_positions():
        t = marks.get(p.pair_id) or {}
        positions.append({"pair_id": p.pair_id, "wsname": p.wsname, "qty": p.qty, "entry": p.entry,
                          "opened_ts": p.opened_ts, "stop_price": p.stop_price, "last": t.get("last"),
                          "bid": t.get("bid"), "ask": t.get("ask")})
    return Mandate(
        cycle_id=cycle_id, mode=h["mode"], venue=venue, created_at=iso(now),
        deadline=iso(now + int(h["seat"]["timeoutSeconds"])), allowed_actions=allowed, budget_usd=budget,
        max_fills=int((cfg.get("live") or {}).get("maxFillsPerCycle") or 1),
        max_submissions=int(h["seat"]["maxSubmissions"]), universe=universe, universe_hash=universe_hash(universe),
        portfolio={"equity_usd": equity, "usd_spot": usd_spot, "invested_usd": invested, "halted": halted,
                   "context_degraded": partial, "positions": positions,
                   "allocated_usd": policy.allocated_usd(cfg), "halt_floor_usd": round(policy.halt_floor_usd(cfg), 2)},
        observe_only=bool(observe), observe_reasons=observe, harness_version=HARNESS_VERSION,
        config_hash=config_mod.config_hash(cfg),
        stop_band={k: h["entry"][k] for k in ("minStopPct", "maxStopPct")}, ticket_cap_usd=cap)


def final_submission(journal, cycle_id: str) -> tuple[int | None, list[dict]]:
    """Last submission where every proposal was schema-valid."""
    rows = journal.proposals_for(cycle_id)
    by_sub: dict[int, list] = {}
    for r in rows:
        by_sub.setdefault(r["submission"], []).append(r)
    for sub in sorted(by_sub, reverse=True):
        props = by_sub[sub]
        if props and all(p["valid"] for p in props):
            return sub, props
    return None, []


def run_cycle(cfg: dict, journal, market, broker, seat, now: float | None = None, skip_seat: bool = False) -> dict:
    now = time.time() if now is None else now
    cycle_id = new_cycle_id(now)
    n = 1
    while journal.q("SELECT 1 FROM runs WHERE cycle_id=?", (cycle_id,)):
        n += 1
        cycle_id = f"{new_cycle_id(now)}-{n}"
    venue = broker.venue
    run_dir = paths.runs_dir() / cycle_id.replace(":", "")
    run_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    receipt: dict = {"cycle_id": cycle_id, "kind": "cycle", "mode": config_mod.mode(cfg), "venue": venue,
                     "harness_version": HARNESS_VERSION, "git_sha": git_sha(), "config_hash": config_mod.config_hash(cfg),
                     "started_at": utc_now(), "errors": [], "orders": [], "approved": 0, "rejected": 0,
                     "seat": None, "run_dir": str(run_dir)}
    journal.run_start(cycle_id, "cycle", config_mod.mode(cfg))
    status = "ok"
    try:
        if venue == "kraken" and config_mod.mode(cfg) != "live":
            raise RuntimeError("kraken broker requires harness.mode=live")
        checks, facts = sensors.preflight(cfg, journal, market, broker, venue, now)
        write_json(run_dir / "sensors.json", checks)
        observe = sensors.observe_only(checks)
        receipt["sensors"] = {c["name"]: c["ok"] for c in checks}
        receipt["observe_only"] = observe
        mandate = build_mandate(cfg, journal, venue, facts, observe, now, cycle_id)
        write_json(run_dir / "mandate.json", mandate.to_dict())
        receipt["universe_hash"] = mandate.universe_hash
        receipt["baseline_pair"] = mandate.universe[0]["pair_id"] if mandate.universe else None
        from .seat.runner import prompt_hash
        receipt["prompt_hash"] = prompt_hash(mandate)
        if observe or skip_seat:
            status = "observe_only" if observe else "skipped_seat"
            write_json(run_dir / "proposals.json", [])
            write_json(run_dir / "gate.json", [])
            write_json(run_dir / "orders.json", [])
        else:
            write_json(paths.active_cycle_path(), {"cycle_id": cycle_id, "mandate_path": str(run_dir / "mandate.json"),
                                                  "deadline": mandate.deadline})
            try:
                seat_res = seat.run(mandate, run_dir)
            finally:
                write_json(paths.active_cycle_path(), {"cycle_id": None, "last": cycle_id})
            receipt["seat"] = seat_res
            receipt["model"] = seat_res.get("model")
            receipt["tool_calls"] = journal.tool_call_count(cycle_id)
            receipt["submissions"] = journal.submission_count(cycle_id)
            sub, props = final_submission(journal, cycle_id)
            if seat_res.get("timed_out"):
                receipt["errors"].append("seat timed out; no trade")
                sub, props = None, []
                status = "seat_timeout"
            elif sub is None:
                receipt["errors"].append("no valid submission; no trade")
                status = "no_submission"
            if sub is not None:
                journal.mark_final(cycle_id, sub)
            proposals = [json.loads(p["raw_json"]) for p in props]
            write_json(run_dir / "proposals.json", {"submission": sub, "proposals": proposals})
            want = sorted({p.get("pair_id") for p in proposals if p.get("pair_id")})
            fresh = market.tickers(want) if want else {}
            state = gatestate.build(cfg, journal, mandate, tickers=fresh, now_epoch=time.time())
            write_json(run_dir / "gate_state.json", gatestate.snapshot(state))
            decisions = policy.gate(proposals, state)
            for p, d in zip(props, decisions):
                d["proposal_id"] = p["id"]
                journal.add_gate(cycle_id, p["id"], False, d)
            write_json(run_dir / "gate.json", decisions)
            receipt["approved"] = sum(1 for d in decisions if d["approved"] and d["action"] != "hold")
            receipt["rejected"] = sum(1 for d in decisions if not d["approved"])
            receipt["holds"] = sum(1 for d in decisions if d["action"] == "hold")
            receipt["reject_reasons"] = sorted({r for d in decisions for r in d["reasons"]})
            orders = execute(cfg, journal, broker, market, mandate, decisions, state) if decisions else []
            receipt["orders"] = orders
            write_json(run_dir / "orders.json", orders)
    except Exception as exc:  # noqa: BLE001
        status = "error"
        receipt["errors"].append(f"{type(exc).__name__}: {exc}")
        (run_dir / "error.txt").write_text(traceback.format_exc())
        journal.add_incident("CYCLE_ERROR", f"{type(exc).__name__}: {exc}"[:1000], None, None, cycle_id)
    receipt["status"] = status
    receipt["finished_at"] = utc_now()
    receipt["duration_s"] = round(time.time() - t0, 2)
    receipt["receipt_hash"] = sha256_obj({k: v for k, v in receipt.items() if k != "receipt_hash"})
    write_json(run_dir / "receipt.json", receipt)
    journal.run_finish(cycle_id, status, str(run_dir / "receipt.json"), receipt)
    return receipt


def replay(cfg: dict, cycle_id: str) -> dict:
    """Re-run the gate on a stored trace with the CURRENT policy. Diff against what happened."""
    run_dir = paths.runs_dir() / cycle_id.replace(":", "")
    from .util import read_json
    snap = read_json(run_dir / "gate_state.json")
    props = read_json(run_dir / "proposals.json") or {}
    old = read_json(run_dir / "gate.json") or []
    if snap is None:
        return {"cycle_id": cycle_id, "error": "no gate_state.json (observe-only or pre-gate failure)"}
    state = gatestate.from_snapshot(cfg, snap)
    new = policy.gate(props.get("proposals") or [], state)
    diffs = []
    for i, (a, b) in enumerate(zip(old, new)):
        if a.get("approved") != b.get("approved") or sorted(a.get("reasons") or []) != sorted(b.get("reasons") or []):
            diffs.append({"index": i, "pair_id": b.get("pair_id"), "was": [a.get("approved"), a.get("reasons")],
                          "now": [b.get("approved"), b.get("reasons")]})
    return {"cycle_id": cycle_id, "proposals": len(new), "diffs": diffs, "same": not diffs}

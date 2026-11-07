"""CLI: python3 -m harness <command>   (or bin/desk <command>)

  cycle [--seat cursor|hold|baseline] [--skip-seat]   one desk cycle (paper unless harness.mode=live)
  exits [--venue paper|kraken]                       mechanical exits (kraken = shadow unless exits.liveKraken)
  eval                                               grade proposals (no model)
  status [--pretty] [--no-balance]                   bar JSON (legacy shape + harness extras)
  doctor [--skip-tests] [--json]                     evidence-backed checklist
  replay <cycle_id>                                  re-run the gate on a stored trace
  halt [reason] | resume                             human halt / resume
  import-legacy [--ledger DIR] [--dry-run] [--kraken]  ledger JSONL -> journal (+ Kraken reconcile)
  context                                            refresh market context
  receipts [-n N]                                    recent run receipts
  notes                                              seat notes
  mcp-serve                                          stdio MCP server for the seat
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config as config_mod, paths


def _journal():
    from .journal import Journal
    return Journal()


def _market(cfg):
    from .market.kraken_public import market_from_env
    return market_from_env(cfg)


def trading_broker(cfg, journal, market):
    """The only place a Kraken broker with orders enabled is built."""
    if config_mod.mode(cfg) == "live":
        from .broker.kraken import KrakenBroker
        return KrakenBroker(cfg, allow_orders=True)
    from .broker.paper import PaperBroker
    return PaperBroker(cfg, journal, market)


def exits_broker(cfg, journal, market, venue):
    if venue == "kraken":
        from .broker.kraken import KrakenBroker
        live = bool((cfg["harness"].get("exits") or {}).get("liveKraken"))
        return KrakenBroker(cfg, allow_orders=live), live
    from .broker.paper import PaperBroker
    return PaperBroker(cfg, journal, market), True


def out(obj, pretty=True):
    print(json.dumps(obj, indent=2 if pretty else None, separators=None if pretty else (",", ":"), default=str))


def cmd_cycle(a, cfg):
    from .cycle import cycle_lock, run_cycle
    from .seat import runner
    j = _journal()
    m = _market(cfg)
    broker = trading_broker(cfg, j, m)
    seat = {"cursor": lambda: runner.CursorSeat(cfg), "hold": lambda: runner.ScriptedSeat(runner.hold_script),
            "baseline": lambda: runner.ScriptedSeat(runner.baseline_script)}[a.seat]()
    if not a.no_context:
        try:
            from .market import context
            context.refresh()
        except Exception as exc:  # noqa: BLE001
            print(f"context refresh failed: {exc}", file=sys.stderr)
    with cycle_lock():
        r = run_cycle(cfg, j, m, broker, seat, skip_seat=a.skip_seat)
    out(r)
    return 0 if r["status"] != "error" else 1


def cmd_exits(a, cfg):
    from .cycle import cycle_lock
    from .exits import run_exits
    j = _journal()
    m = _market(cfg)
    venue = a.venue or ("kraken" if config_mod.mode(cfg) == "live" else "paper")
    if venue == "kraken":
        from .legacy import import_legacy
        import_legacy(j, cfg)
    broker, do_sell = exits_broker(cfg, j, m, venue)
    with cycle_lock(f"desk-exits-{venue}.lock"):
        r = run_exits(cfg, j, m, broker, venue, do_sell=do_sell)
    if a.brief:
        r = {k: v for k, v in r.items() if k != "results"} | {
            "results": [{k: x.get(k) for k in ("pair_id", "action", "rule_fired", "stop", "last", "legacy_decision")}
                        for x in r["results"]]}
    out(r)
    return 0


def cmd_eval(a, cfg):
    from .evaluate import run_eval
    out(run_eval(cfg, _journal(), _market(cfg)))
    return 0


def cmd_status(a, cfg):
    from .status import build_status
    j = _journal()
    m = _market(cfg)
    kb = None
    if not a.no_balance:
        from .broker.kraken import KrakenBroker
        kb = KrakenBroker(cfg)  # query-only
    from .broker.paper import PaperBroker
    s = build_status(cfg, j, m, kraken_broker=kb, paper_broker=PaperBroker(cfg, j, m))
    out(s, pretty=a.pretty)
    return 0


def cmd_doctor(a, cfg):
    from .doctor import render, run_doctor
    items = run_doctor(cfg, _journal(), skip_tests=a.skip_tests)
    if a.json:
        out(items)
    else:
        print(render(items))
    return 0 if all(i["ok"] is not False for i in items) else 1


def cmd_replay(a, cfg):
    from .cycle import replay
    out(replay(cfg, a.cycle_id))
    return 0


def cmd_halt(a, cfg):
    from .util import utc_now
    j = _journal()
    row = {"ts": utc_now(), "status": "HALTED", "reason": a.reason or "manual", "source": "human", "resume": "human"}
    j.set_halt(row)
    j.add_incident("HALT_MANUAL", json.dumps(row))
    out(row)
    return 0


def cmd_resume(a, cfg):
    from .sensors import legacy_halted
    j = _journal()
    was = j.halt_state()
    j.set_halt(None)
    j.event("resume", None, {"was": was})
    res = {"resumed": True, "was": was}
    if legacy_halted():
        res["note"] = "ledger/halt.json is still HALTED (legacy desk); clear it by hand if intended"
    out(res)
    return 0


def cmd_import_legacy(a, cfg):
    from .legacy import import_legacy, reconcile_kraken
    j = _journal()
    r = import_legacy(j, cfg, Path(a.ledger) if a.ledger else None, dry_run=a.dry_run)
    if a.kraken and not a.dry_run:
        from .broker.kraken import KrakenBroker
        r["reconcile"] = reconcile_kraken(j, cfg, KrakenBroker(cfg), _market(cfg))
    out(r)
    return 0 if r.get("legacy_crosscheck") != "MISMATCH" else 2


def cmd_context(a, cfg):
    from .market import context
    p = context.refresh()
    out({"fetchedAt": p["fetchedAt"], "degraded": p["degraded"], "errors": p["errors"], "signals": len(p["signals"])})
    return 0


def cmd_receipts(a, cfg):
    j = _journal()
    rows = j.q("SELECT cycle_id, kind, status, started_at, finished_at, receipt_json FROM runs WHERE kind=? "
               "ORDER BY started_at DESC LIMIT ?", (a.kind, a.n))
    res = []
    for r in rows:
        rec = json.loads(r["receipt_json"] or "{}")
        res.append({"cycle_id": r["cycle_id"], "status": r["status"], "finished_at": r["finished_at"],
                    "approved": rec.get("approved"), "rejected": rec.get("rejected"),
                    "observe_only": rec.get("observe_only"), "orders": len(rec.get("orders") or []),
                    "errors": rec.get("errors"), "closes": rec.get("closes")})
    out(res)
    return 0


def cmd_notes(a, cfg):
    out(_journal().notes(20))
    return 0


def cmd_mcp_serve(a, cfg):
    from .seat.mcp_server import serve
    return serve()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="desk", description="Trading desk harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("cycle")
    s.add_argument("--seat", choices=["cursor", "hold", "baseline"], default="cursor")
    s.add_argument("--skip-seat", action="store_true")
    s.add_argument("--no-context", action="store_true")
    s.set_defaults(func=cmd_cycle)
    s = sub.add_parser("exits")
    s.add_argument("--venue", choices=["paper", "kraken"])
    s.add_argument("--brief", action="store_true")
    s.set_defaults(func=cmd_exits)
    sub.add_parser("eval").set_defaults(func=cmd_eval)
    s = sub.add_parser("status")
    s.add_argument("--pretty", action="store_true")
    s.add_argument("--no-balance", action="store_true")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("doctor")
    s.add_argument("--skip-tests", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("replay")
    s.add_argument("cycle_id")
    s.set_defaults(func=cmd_replay)
    s = sub.add_parser("halt")
    s.add_argument("reason", nargs="?")
    s.set_defaults(func=cmd_halt)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    s = sub.add_parser("import-legacy")
    s.add_argument("--ledger")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--kraken", action="store_true", help="also reconcile against Kraken balances (query-only)")
    s.set_defaults(func=cmd_import_legacy)
    sub.add_parser("context").set_defaults(func=cmd_context)
    s = sub.add_parser("receipts")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--kind", default="cycle")
    s.set_defaults(func=cmd_receipts)
    sub.add_parser("notes").set_defaults(func=cmd_notes)
    sub.add_parser("mcp-serve").set_defaults(func=cmd_mcp_serve)
    a = p.parse_args(argv)
    cfg = config_mod.load()
    return a.func(a, cfg)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/python3 -I
"""Hourly desk cycle. Mechanical SCAN/VET/SIZE/FILLS; the LLM may only veto.

  shortlist      universe + ticker, gates, book guards -> ledger/shortlist.json
  record-vetoes  validate the LLM verdicts on stdin -> ledger/vetoes.json
  execute        balance, reconcile, size, buy (or shadow) -> reports.jsonl

A missing or mismatched vetoes.json means no buys this hour.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import desk_book  # noqa: E402
import desk_halt  # noqa: E402
import desk_rules as rules  # noqa: E402
import kraken_universe as ku  # noqa: E402
from desk_paths import ledger_dir  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
SHORTLIST_MAX_AGE_S = 45 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def venue(cfg: dict) -> str:
    if cfg.get("paper"):
        return "paper"
    return (cfg.get("venues") or {}).get("fills") or "paper"


def append(name: str, row: dict) -> None:
    path = ledger_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def write_json(name: str, payload: dict) -> None:
    path = ledger_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def read_json(name: str) -> dict | None:
    path = ledger_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def context_degraded() -> bool:
    ctx = read_json("context.json")
    if not ctx:
        return True
    return bool(ctx.get("degraded") or ctx.get("unavailable"))


def candidate_row(pair: dict, m: dict) -> dict:
    return {
        "pair_id": pair["pair_id"],
        "wsname": pair.get("wsname") or "",
        "altname": pair.get("altname") or "",
        "base": rules.pair_base(pair),
        "last": m["last"],
        "ask": m["ask"],
        "bid": m["bid"],
        "spread": m["spread"],
        "pct_change": m["pct_change"],
        "high_24h": m.get("high_24h"),
        "low_24h": m.get("low_24h"),
        "volume_24h_quote": m["volume_24h_quote"],
        "trades_24h": m["trades_24h"],
        "ordermin": pair.get("ordermin"),
        "costmin": pair.get("costmin"),
        "min_ticket_usd": ku.min_ticket_usd({**pair, "last": m["last"]}),
        "taker_fee": pair.get("taker_fee"),
    }


def build_shortlist(cfg: dict, universe: dict, tickers: dict, book, now: float, cycle_id: str) -> dict:
    index = ku.pair_index(universe)
    passed: list[dict] = []
    blocked: list[dict] = []
    gates: dict[str, int] = {}
    for pid, m in tickers.items():
        pair = index.get(pid)
        if not pair:
            continue
        failure = rules.scan_failure(pair, m, cfg)
        if failure:
            gates[failure] = gates.get(failure, 0) + 1
            continue
        row = candidate_row(pair, m)
        block = rules.entry_block(pid, book, cfg, now)
        if block:
            gates[block] = gates.get(block, 0) + 1
            blocked.append({**row, "failed_check": block})
            continue
        passed.append(row)
    passed.sort(key=lambda r: (-float(r["volume_24h_quote"]), -int(r["trades_24h"])))
    limit = int((cfg.get("scan") or {}).get("maxCandidates", 10))
    shortlist = passed[:limit]
    for i, row in enumerate(shortlist, start=1):
        row["rank"] = i
    return {
        "ts": utc_now(),
        "cycle_id": cycle_id,
        "degraded": context_degraded(),
        "candidates": shortlist,
        "blocked": blocked,
        "gates": gates,
        "universe_count": len(index),
    }


def cmd_shortlist(args: argparse.Namespace) -> int:
    cfg = load_config()
    universe = ku.fetch_universe(cfg)
    tickers = ku.fetch_tickers([p["pair_id"] for p in universe.get("pairs") or []])
    book = desk_book.load_book(venue(cfg))
    payload = build_shortlist(cfg, universe, tickers, book, time.time(), args.cycle_id)
    write_json("shortlist.json", payload)
    write_json("vetoes.json", {"cycle_id": None, "note": "awaiting desk-vet"})
    for row in payload["candidates"]:
        append("candidates.jsonl", {"ts": payload["ts"], "cycle_id": args.cycle_id, **row})
    for row in payload["blocked"]:
        append(
            "rejections.jsonl",
            {"ts": payload["ts"], "cycle_id": args.cycle_id, "pair_id": row["pair_id"], "wsname": row["wsname"],
             "verdict": "REJECT", "failed_check": row["failed_check"], "source": "desk_cycle"},
        )
    print(json.dumps({
        "cycle_id": args.cycle_id,
        "candidates": [c["pair_id"] for c in payload["candidates"]],
        "blocked": {r["pair_id"]: r["failed_check"] for r in payload["blocked"]},
        "gates": payload["gates"],
        "degraded": payload["degraded"],
    }, indent=2))
    return 0


def validate_vetoes(shortlist: dict, submitted: dict) -> dict:
    """Every shortlisted pair gets a verdict. Missing or unknown verdicts are vetoes."""
    if submitted.get("cycle_id") != shortlist.get("cycle_id"):
        raise ValueError(f"cycle_id {submitted.get('cycle_id')!r} does not match shortlist {shortlist.get('cycle_id')!r}")
    by_pair: dict[str, dict] = {}
    known = {c["pair_id"] for c in shortlist.get("candidates") or []}
    for row in submitted.get("vetoes") or []:
        pid = row.get("pair_id")
        if pid not in known:
            continue
        veto = row.get("veto")
        by_pair[pid] = {
            "pair_id": pid,
            "veto": veto is not False,
            "why": str(row.get("why") or "")[:500],
            "evidence": row.get("evidence"),
        }
    for pid in known - set(by_pair):
        by_pair[pid] = {"pair_id": pid, "veto": True, "why": "no verdict from desk-vet", "evidence": None}
    return {
        "ts": utc_now(),
        "cycle_id": shortlist["cycle_id"],
        "vetoes": [by_pair[c["pair_id"]] for c in shortlist.get("candidates") or []],
    }


def cmd_record_vetoes(args: argparse.Namespace) -> int:
    shortlist = read_json("shortlist.json")
    if not shortlist:
        print(json.dumps({"error": "no shortlist.json"}), file=sys.stderr)
        return 1
    try:
        submitted = json.loads(sys.stdin.read())
        payload = validate_vetoes(shortlist, submitted)
    except (json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    write_json("vetoes.json", payload)
    for row in payload["vetoes"]:
        if row["veto"]:
            append(
                "rejections.jsonl",
                {"ts": payload["ts"], "cycle_id": payload["cycle_id"], "pair_id": row["pair_id"],
                 "verdict": "REJECT", "failed_check": "LLM_VETO", "why": row["why"], "evidence": row["evidence"],
                 "source": "desk-vet"},
            )
    print(json.dumps(payload, indent=2))
    return 0


def kraken_balance() -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "kraken_balance.py")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "balance failed").strip()[-300:])
    return json.loads(proc.stdout)


def run_buy(c: dict, dollars: float, cycle_id: str) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "kraken_execute.py"),
            "buy",
            "--pair", c["pair_id"],
            "--wsname", c.get("wsname") or "",
            "--dollars", f"{dollars:.2f}",
            "--decision-price", str(c.get("last") or 0),
            "--cycle-id", cycle_id,
        ],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(ROOT),
    )
    out = (proc.stdout or proc.stderr or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"raw": out[-400:]}
    payload["exit_code"] = proc.returncode
    return payload


def plan_entries(
    cfg: dict,
    shortlist: dict,
    vetoes: dict | None,
    book,
    equity: float,
    usd_spot: float,
    halted: bool,
    now: float,
) -> list[dict]:
    """One decision per shortlisted pair. dollars > 0 only for the pair(s) to buy."""
    max_fills = int((cfg.get("live") or {}).get("maxFillsPerCycle") or 1)
    vet_ok = bool(vetoes) and vetoes.get("cycle_id") == shortlist.get("cycle_id")
    verdicts = {v["pair_id"]: v for v in (vetoes or {}).get("vetoes") or []} if vet_ok else {}
    partial = bool(shortlist.get("degraded"))
    invested = max(0.0, equity - usd_spot)
    planned = 0
    out = []
    for c in shortlist.get("candidates") or []:
        pid = c["pair_id"]
        row = {"pair_id": pid, "wsname": c.get("wsname"), "rank": c.get("rank"), "dollars": 0.0}
        verdict = verdicts.get(pid)
        if not vet_ok:
            row["reason"] = "VET_MISSING"
        elif not verdict or verdict.get("veto"):
            row["reason"] = "LLM_VETO"
            row["why"] = (verdict or {}).get("why")
        elif halted:
            row["reason"] = "HALTED"
        elif planned >= max_fills:
            row["reason"] = "MAX_FILLS_PER_CYCLE"
        else:
            block = rules.entry_block(pid, book, cfg, now)
            if block:
                row["reason"] = block
            else:
                dollars, why = rules.ticket_usd(
                    equity, usd_spot, invested, float(c.get("min_ticket_usd") or 0), cfg, partial=partial
                )
                row["dollars"] = dollars
                row["reason"] = why or ("PASS_PARTIAL" if partial else "PASS")
                if dollars > 0:
                    planned += 1
                    invested += dollars
                    usd_spot -= dollars
        out.append(row)
    return out


def cmd_execute(args: argparse.Namespace) -> int:
    cfg = load_config()
    cycle_id = args.cycle_id
    now = time.time()
    ts = utc_now()
    shortlist = read_json("shortlist.json") or {}
    errors: list[str] = []
    if shortlist.get("cycle_id") != cycle_id:
        errors.append("shortlist missing or from another cycle")
        shortlist = {"cycle_id": cycle_id, "candidates": []}
    elif now - (rules.parse_ts(shortlist.get("ts")) or 0) > SHORTLIST_MAX_AGE_S:
        errors.append("shortlist stale")
        shortlist = {**shortlist, "candidates": []}
    vetoes = read_json("vetoes.json")
    book = desk_book.load_book(venue(cfg))

    bal: dict = {}
    try:
        bal = kraken_balance()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        errors.append(f"balance: {exc}")
    equity = float(bal.get("equivalent_usd") or 0)
    usd_spot = float(bal.get("usd_spot") or 0)
    halted = desk_halt.is_halted() or bool(bal.get("halted"))

    rec = None
    if bal:
        universe = ku.load_universe()
        held_pairs = [
            desk_book.asset_pair_index(universe).get(str(a.get("asset") or "").upper(), {}).get("pair_id")
            for a in bal.get("leftover_assets") or []
        ]
        held_pairs = [p for p in held_pairs if p]
        try:
            lasts = {pid: t["last"] for pid, t in ku.fetch_tickers(held_pairs).items()} if held_pairs else {}
        except Exception:
            lasts = {}
        rec = desk_book.reconcile(book, bal.get("leftover_assets") or [], universe, lasts)
        desk_book.write_reconcile(rec)

    if not bal:
        decisions = [{"pair_id": c["pair_id"], "dollars": 0.0, "reason": "NO_BALANCE"} for c in shortlist["candidates"]]
    else:
        decisions = plan_entries(cfg, shortlist, vetoes, book, equity, usd_spot, halted, now)

    enabled = bool((cfg.get("entry") or {}).get("enabled"))
    fills = []
    shadow = []
    for d in decisions:
        append("sizes.jsonl", {"ts": ts, "cycle_id": cycle_id, "shadow": not enabled, **d})
        if d["dollars"] <= 0:
            continue
        cand = next(c for c in shortlist["candidates"] if c["pair_id"] == d["pair_id"])
        if not enabled:
            row = {"ts": ts, "cycle_id": cycle_id, "pair_id": d["pair_id"], "wsname": d.get("wsname"),
                   "dollars": d["dollars"], "ask": cand.get("ask"), "last": cand.get("last")}
            append("shadow.jsonl", row)
            shadow.append(row)
            continue
        result = run_buy(cand, d["dollars"], cycle_id)
        d["fill"] = result
        if result.get("exit_code") == 0:
            fills.append(result)

    vet_rows = ((vetoes or {}).get("vetoes") or []) if (vetoes or {}).get("cycle_id") == cycle_id else []
    report = {
        "ts": utc_now(),
        "cycle_id": cycle_id,
        "kind": "cycle",
        "paper": bool(cfg.get("paper")),
        "entries_enabled": enabled,
        "halted": halted,
        "degraded": bool(shortlist.get("degraded")),
        "usd_spot": round(usd_spot, 2) if bal else None,
        "equity_usd": round(equity, 2) if bal else None,
        "allocated_usd": desk_halt.allocated_usd(cfg),
        "open_count": len(book.positions),
        "scan": len(shortlist.get("candidates") or []),
        "vet_pass": sum(1 for v in vet_rows if not v.get("veto")),
        "vet_reject": sum(1 for v in vet_rows if v.get("veto")),
        "sizes": sum(1 for d in decisions if d["dollars"] > 0),
        "fills": len(fills),
        "shadow_fills": len(shadow),
        "candidates": [
            {k: c.get(k) for k in ("pair_id", "wsname", "rank", "last", "pct_change", "spread", "volume_24h_quote")}
            for c in shortlist.get("candidates") or []
        ],
        "decisions": decisions,
        "reconcile": rec,
        "errors": errors,
        "note": "desk_cycle",
    }
    append("reports.jsonl", report)
    print(json.dumps(report, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hourly desk cycle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shortlist")
    s.add_argument("--cycle-id", required=True)
    s.set_defaults(func=cmd_shortlist)
    v = sub.add_parser("record-vetoes")
    v.set_defaults(func=cmd_record_vetoes)
    e = sub.add_parser("execute")
    e.add_argument("--cycle-id", required=True)
    e.set_defaults(func=cmd_execute)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

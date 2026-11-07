"""import-legacy: load ledger/fills.jsonl + closes.jsonl into the journal.

Episode semantics match scripts/desk_book.load_book. For every open legacy position the
stop the legacy desk is actually enforcing (entry * (1 - risk.stopLossPct), the formula in
scripts/desk_rules.stop_price) is frozen ONCE into the stops table with source
"legacy_import". Re-imports never overwrite an existing stop, so a later config change or
the new ask-based rule can never move a live stop.

Idempotent: rows dedupe on txid (or a hash of the raw row). Legacy venue "paper" rows
become venue "legacy-paper" so they never mix with the harness paper book.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from . import paths, policy
from .book import build_book, load_book
from .util import load_jsonl, read_json, sha256_obj, utc_now

VENUE_MAP = {"paper": "legacy-paper"}


def _venue(row: dict) -> str:
    v = row.get("venue") or "paper"
    return VENUE_MAP.get(v, v)


def legacy_is_close_row(row: dict) -> bool:
    if str(row.get("action") or "").upper() != "CLOSE":
        return False
    return str(row.get("status") or "").lower() not in {"canceled", "cancelled", "expired"}


def fill_row(r: dict) -> dict:
    return {"ts": str(r.get("ts") or ""), "cycle_id": r.get("cycle_id"), "venue": _venue(r), "pair_id": r["pair_id"],
            "wsname": r.get("wsname") or "", "qty": float(r.get("volume_base") or 0), "cost_usd": float(r.get("filled") or 0),
            "price": r.get("fill_price"), "fee_usd": float(r.get("fee_usd") or 0), "txid": r.get("txid"),
            "source": "legacy", "dedupe": "legacy-fill:" + (r.get("txid") or sha256_obj(r)), "raw": r}


def close_row(r: dict) -> dict:
    return {"ts": str(r.get("ts") or ""), "cycle_id": r.get("cycle_id"), "venue": _venue(r), "pair_id": r["pair_id"],
            "wsname": r.get("wsname") or "", "qty": float(r.get("volume_base") or 0),
            "proceeds_usd": float(r.get("filled") or 0), "price": r.get("fill_price"), "fee_usd": float(r.get("fee_usd") or 0),
            "rule_fired": r.get("rule_fired"), "txid": r.get("txid"), "status": r.get("status") or "closed",
            "source": "legacy", "dedupe": "legacy-close:" + (r.get("txid") or sha256_obj(r)), "raw": r}


def legacy_desk_book(ledger: Path, venue: str = "kraken"):
    """Run the real scripts/desk_book.load_book (if present) for a cross-check."""
    path = paths.ROOT / "scripts" / "desk_book.py"
    if not path.exists():
        return None
    scripts = str(path.parent)
    added = scripts not in sys.path
    if added:
        sys.path.insert(0, scripts)
    old = os.environ.get("TRADING_DESK_LEDGER")
    os.environ["TRADING_DESK_LEDGER"] = str(ledger)
    try:
        spec = importlib.util.spec_from_file_location("_legacy_desk_book", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.load_book(venue, ledger)
    except Exception:
        return None
    finally:
        if old is None:
            os.environ.pop("TRADING_DESK_LEDGER", None)
        else:
            os.environ["TRADING_DESK_LEDGER"] = old
        if added:
            sys.path.remove(scripts)


def import_legacy(journal, cfg: dict, ledger: Path | None = None, dry_run: bool = False) -> dict:
    ledger = ledger or paths.ledger_dir()
    raw_fills = [r for r in load_jsonl(ledger / "fills.jsonl") if r.get("pair_id")]
    raw_closes_all = [r for r in load_jsonl(ledger / "closes.jsonl") if r.get("pair_id")]
    raw_closes = [r for r in raw_closes_all if legacy_is_close_row(r)]
    fills = [fill_row(r) for r in raw_fills]
    closes = [close_row(r) for r in raw_closes]

    # Book as the harness will see it (kraken venue), built from the rows we import.
    kf = [f for f in fills if f["venue"] == "kraken"]
    kc = [c for c in closes if c["venue"] == "kraken"]
    preview = build_book("kraken", kf, kc)

    # Cross-check against the legacy module itself.
    legacy = legacy_desk_book(ledger, "kraken")
    mismatches = []
    if legacy is not None:
        lp = {p.pair_id: p for p in legacy.open_positions()}
        hp = preview.positions
        for pid in sorted(set(lp) | set(hp)):
            a, b = lp.get(pid), hp.get(pid)
            if a is None or b is None:
                mismatches.append({"pair_id": pid, "legacy": bool(a), "harness": bool(b)})
                continue
            if abs(a.qty - b.qty) > 1e-9 or abs(a.cost_usd - b.cost_usd) > 1e-6 or a.opened_ts != b.opened_ts:
                mismatches.append({"pair_id": pid, "legacy": [a.qty, a.cost_usd, a.opened_ts],
                                   "harness": [b.qty, b.cost_usd, b.opened_ts]})
        if len(legacy.closed) != len(preview.closed):
            mismatches.append({"closed_episodes": [len(legacy.closed), len(preview.closed)]})

    rec = read_json(ledger / "reconcile.json", {}) or {}
    untracked = rec.get("untracked") or []

    summary = {"ts": utc_now(), "ledger": str(ledger), "dry_run": dry_run, "fills_seen": len(fills),
               "closes_seen": len(closes), "closes_skipped_not_close": len(raw_closes_all) - len(raw_closes),
               "fills_inserted": 0, "closes_inserted": 0, "stops_frozen": [], "stops_kept": [],
               "open_positions": [], "legacy_crosscheck": "ok" if legacy is not None and not mismatches
               else ("unavailable" if legacy is None else "MISMATCH"), "mismatches": mismatches,
               "legacy_untracked": untracked}
    if dry_run:
        for p in preview.open_positions():
            summary["open_positions"].append({"pair_id": p.pair_id, "wsname": p.wsname, "qty": p.qty,
                                              "entry": p.entry, "opened_ts": p.opened_ts,
                                              "stop_price": policy.legacy_stop_price(p.entry, cfg)})
        return summary

    for f in fills:
        if journal.add_fill(f) is not None:
            summary["fills_inserted"] += 1
    for c in closes:
        if journal.add_close(c) is not None:
            summary["closes_inserted"] += 1

    book = load_book(journal, "kraken")
    for p in book.open_positions():
        existing = journal.first_stop("kraken", p.pair_id, p.opened_ts)
        if existing is None:
            stop = policy.legacy_stop_price(p.entry, cfg)
            journal.set_stop("kraken", p.pair_id, p.opened_ts, stop, "legacy_import")
            summary["stops_frozen"].append({"pair_id": p.pair_id, "stop_price": stop})
        else:
            summary["stops_kept"].append({"pair_id": p.pair_id, "stop_price": existing["stop_price"]})
    book = load_book(journal, "kraken")
    for p in book.open_positions():
        summary["open_positions"].append({"pair_id": p.pair_id, "wsname": p.wsname, "qty": p.qty, "entry": p.entry,
                                          "opened_ts": p.opened_ts, "stop_price": p.stop_price,
                                          "stop_source": p.stop_source})
    journal.meta_set("legacy_import", __import__("json").dumps(
        {k: summary[k] for k in ("ts", "fills_seen", "closes_seen", "fills_inserted", "closes_inserted",
                                 "legacy_crosscheck")} | {"open": len(summary["open_positions"])}))
    if mismatches:
        journal.add_incident("LEGACY_IMPORT_MISMATCH", __import__("json").dumps(mismatches)[:2000],
                             "legacy_crosscheck", "test_import_matches_legacy_desk_book")
    return summary


def reconcile_kraken(journal, cfg: dict, broker, market) -> dict:
    """Compare the journal kraken book with Kraken balances (query-only). Stores meta reconcile:kraken."""
    from .book import reconcile
    bal = broker.balance()
    universe = market.universe()
    pairs = universe.get("pairs") or []
    book = load_book(journal, "kraken")
    from .book import asset_pair_index
    idx = asset_pair_index(pairs)
    held = [idx.get(str(a.get("asset") or "").upper(), {}).get("pair_id") for a in bal.get("leftover_assets") or []]
    held = [h for h in held if h]
    try:
        lasts = {pid: t["last"] for pid, t in market.tickers(held).items()} if held else {}
    except Exception:
        lasts = {}
    rec = reconcile(book, bal.get("leftover_assets") or [], pairs, lasts)
    rec["equity_usd"] = bal.get("equity_usd")
    rec["usd_spot"] = bal.get("usd_spot")
    journal.meta_set("reconcile:kraken", __import__("json").dumps(rec, default=str))
    return rec

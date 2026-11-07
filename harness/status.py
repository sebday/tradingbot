"""Bar/panel JSON. Same keys as scripts/desk_status.py (BarWidget.qml, Panel.qml, Model.js),
plus additive keys: harness, last_receipt, gate.

Positions shown are the real-money (kraken) book synced from the legacy ledger, with stops
read from the journal (frozen at import). The paper book is summarised under harness.paper.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config as config_mod, paths, policy, sensors
from .book import load_book
from .util import read_json, utc_now


def brief_error(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "balance failed"
    for line in reversed(raw.splitlines()):
        line = line.strip().removeprefix("RuntimeError:").strip()
        if line:
            return line[:180]
    return "balance failed"


def _reports_rows_reversed(path):
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-2000:]):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def latest_amounts(rows) -> tuple:
    for row in rows:
        if row.get("kind") == "exit" or row.get("equity_usd") is None:
            continue
        try:
            spot = row.get("usd_spot")
            return float(row["equity_usd"]), (float(spot) if spot is not None else None)
        except (TypeError, ValueError):
            continue
    return None, None


def runner_state(journal) -> dict:
    meta = read_json(paths.legacy_state_dir() / "last_run.json", {}) or {}
    last = journal.last_run("cycle")
    if last and (last.get("started_at") or "") > (meta.get("started_at") or ""):
        meta = {"started_at": last["started_at"], "finished_at": last["finished_at"],
                "exit_code": 0 if last["status"] in ("ok", "observe_only", "skipped_seat", "no_submission") else 1,
                "error": None if last["status"] == "ok" else last["status"], "running": last["status"] == "running"}
    running = bool(meta.get("running"))
    if meta.get("finished_at") in (None, "") and meta.get("started_at"):
        running = True
    stale = False
    last_ts = meta.get("finished_at") or meta.get("started_at")
    if last_ts:
        try:
            t = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - t).total_seconds() / 60.0 > 70
        except ValueError:
            stale = True
    return {"running": running, "stale": stale, "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"), "exit_code": meta.get("exit_code"), "error": meta.get("error")}


def gate_summary(journal, cycle_id: str | None) -> dict | None:
    if not cycle_id:
        return None
    rows = journal.q("SELECT approved, reasons_json FROM gate_decisions WHERE cycle_id=? AND dry_run=0", (cycle_id,))
    reasons: dict = {}
    for r in rows:
        for x in json.loads(r["reasons_json"] or "[]"):
            reasons[x] = reasons.get(x, 0) + 1
    return {"cycle_id": cycle_id, "approved": sum(1 for r in rows if r["approved"]),
            "rejected": sum(1 for r in rows if not r["approved"]), "reasons": reasons}


def build_status(cfg: dict, journal, market, kraken_broker=None, paper_broker=None, sync_legacy: bool = True) -> dict:
    if sync_legacy:
        try:
            from .legacy import import_legacy
            import_legacy(journal, cfg)
        except Exception:  # noqa: BLE001
            pass
    allocated = policy.allocated_usd(cfg)
    floor = policy.halt_floor_usd(cfg)
    halted = sensors.is_halted(journal) is not None
    book = load_book(journal, "kraken")
    opens = book.open_positions()

    equity = usd_spot = None
    balance_err = None
    if kraken_broker is not None:
        try:
            bal = kraken_broker.balance()
            equity, usd_spot = bal.get("equity_usd"), bal.get("usd_spot")
        except Exception as exc:  # noqa: BLE001
            balance_err = brief_error(f"{type(exc).__name__}: {exc}")
    pids = [p.pair_id for p in opens]
    try:
        lasts = {k: v.get("last") for k, v in market.tickers(pids).items()} if pids else {}
    except Exception:  # noqa: BLE001
        lasts = {}

    positions = []
    for p in opens:
        last = lasts.get(p.pair_id)
        entry = p.entry
        pnl = pct = None
        if last is not None and p.qty > 0 and entry > 0:
            pnl = round((last - entry) * p.qty, 2)
            pct = round((last / entry - 1.0) * 100.0, 2)
        stop = p.stop_price if p.stop_price is not None else (policy.legacy_stop_price(entry, cfg) if entry else None)
        positions.append({"pair_id": p.pair_id, "wsname": p.wsname, "qty": p.qty,
                          "entry": round(entry, 8) if entry else 0, "filled_usd": round(p.cost_usd, 2), "last": last,
                          "pnl_usd": pnl, "pct_vs_entry": pct, "stop_price": round(stop, 8) if stop else None,
                          "stop_source": p.stop_source or "computed"})

    rows = _reports_rows_reversed(paths.ledger_dir() / "reports.jsonl")
    last_cycle = next((r for r in rows if r.get("kind") != "exit"), rows[0] if rows else None)
    if equity is None or usd_spot is None:
        pe, ps = latest_amounts(rows)
        equity = pe if equity is None else equity
        usd_spot = ps if usd_spot is None else usd_spot

    bar_text = ""
    if equity is not None:
        try:
            n = float(equity)
            whole = int(round(abs(n)))
            bar_text = f"-${whole:,}" if n < 0 else f"${whole:,}"
        except (TypeError, ValueError):
            bar_text = ""

    last_run = journal.last_run("cycle")
    last_receipt = json.loads(last_run["receipt_json"]) if last_run and last_run.get("receipt_json") else None
    receipt_brief = None
    if last_receipt:
        receipt_brief = {k: last_receipt.get(k) for k in ("cycle_id", "status", "mode", "venue", "approved", "rejected",
                                                          "observe_only", "errors", "finished_at", "model", "tool_calls",
                                                          "duration_s", "run_dir")}
        receipt_brief["orders"] = len(last_receipt.get("orders") or [])
    paper = None
    if paper_broker is not None:
        try:
            pb = paper_broker.balance()
            paper = {"equity_usd": pb["equity_usd"], "usd_spot": pb["usd_spot"],
                     "open_count": len(load_book(journal, "paper").positions)}
        except Exception as exc:  # noqa: BLE001
            paper = {"error": str(exc)[:200]}

    out = {
        "class": "ok", "ok": True, "text": bar_text,
        "tooltip": f"Desk equity {bar_text}" if bar_text else "Kraken desk",
        "equity_usd": equity, "usd_spot": usd_spot, "allocated_usd": round(allocated, 2),
        "halt_floor_usd": round(floor, 2), "halted": halted, "open_count": len(positions), "positions": positions,
        "last_cycle": last_cycle, "runner": runner_state(journal), "balance_error": balance_err,
        "desk_root": str(paths.ROOT), "fetched_at": utc_now(),
        # additive
        "harness": {"mode": config_mod.mode(cfg), "paper": paper, "exits_last": (journal.last_run("exits") or {}).get("started_at")},
        "last_receipt": receipt_brief,
        "gate": gate_summary(journal, (last_receipt or {}).get("cycle_id")),
    }
    if not bar_text and balance_err and not positions:
        out["ok"] = False
        out["class"] = "error"
        out["error"] = balance_err
    return out

#!/usr/bin/env python3
"""Human halt: lose haltDrawdown of allocatedUsd, stop new fills, wait."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ledger_dir() -> Path:
    return Path(os.environ.get("TRADING_DESK_LEDGER") or Path.home() / ".local/state/omarchy/trading/ledger")


def halt_path() -> Path:
    return ledger_dir() / "halt.json"


def load_halt() -> dict | None:
    path = halt_path()
    if not path.exists():
        return None
    return json.loads(path.read_text())


def is_halted() -> bool:
    row = load_halt()
    return bool(row) and row.get("status") == "HALTED"


def allocated_usd(cfg: dict) -> float:
    bank = cfg.get("bank") or {}
    if bank.get("allocatedUsd") is not None:
        return float(bank["allocatedUsd"])
    return float(bank.get("startingUsd") or 0)


def halt_floor_usd(cfg: dict) -> float:
    drawdown = float((cfg.get("risk") or {}).get("haltDrawdown") or 0.5)
    return allocated_usd(cfg) * (1.0 - drawdown)


def should_trip(equity_usd: float, cfg: dict) -> bool:
    if not (cfg.get("risk") or {}).get("haltUntilHuman", True):
        return False
    return float(equity_usd) <= halt_floor_usd(cfg) + 1e-9


def write_halt(reason: str, equity_usd: float | None, cfg: dict, source: str) -> dict:
    row = {
        "ts": utc_now(),
        "status": "HALTED",
        "reason": reason,
        "source": source,
        "equity_usd": None if equity_usd is None else round(float(equity_usd), 2),
        "allocated_usd": round(allocated_usd(cfg), 2),
        "floor_usd": round(halt_floor_usd(cfg), 2),
        "resume": "human",
        "note": "No new fills until you clear ledger/halt.json",
    }
    path = halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2) + "\n")
    return row


def trip_if_needed(equity_usd: float, cfg: dict, source: str) -> dict | None:
    if is_halted():
        return load_halt()
    if not should_trip(equity_usd, cfg):
        return None
    return write_halt(
        f"equity {equity_usd:.2f} at or below {halt_floor_usd(cfg):.2f} "
        f"({100 * float((cfg.get('risk') or {}).get('haltDrawdown') or 0.5):.0f}% of pot)",
        equity_usd,
        cfg,
        source,
    )

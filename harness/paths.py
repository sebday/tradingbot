"""Filesystem layout for the harness. Everything overridable by env for tests."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


def config_path() -> Path:
    return _env_path("DESK_CONFIG", ROOT / "config.json")


def state_dir() -> Path:
    return _env_path("DESK_STATE_DIR", ROOT / "state")


def db_path() -> Path:
    return _env_path("DESK_DB", state_dir() / "desk.db")


def runs_dir() -> Path:
    return _env_path("DESK_RUNS_DIR", ROOT / "runs")


def ledger_dir() -> Path:
    """Legacy ledger (read-only for the harness, except import-legacy reading it)."""
    raw = os.environ.get("DESK_LEDGER", "").strip() or os.environ.get("TRADING_DESK_LEDGER", "").strip()
    return Path(raw) if raw else ROOT / "ledger"


def seat_dir() -> Path:
    return _env_path("DESK_SEAT_DIR", ROOT / "seat")


def active_cycle_path() -> Path:
    return state_dir() / "active-cycle.json"


def legacy_state_dir() -> Path:
    env = os.environ.get("EVOSHELL_STATE", "").strip()
    if env:
        return Path(env) / "trading"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    return Path(xdg or Path.home() / ".local/state") / "evoshell" / "trading"

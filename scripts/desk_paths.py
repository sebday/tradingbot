"""Shared paths for the Kraken desk. Runtime data lives in $EVOSHELL_STATE/trading."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_NAME = "evo-trading-desk.lock"


def evoshell_state_dir() -> Path:
    env = os.environ.get("EVOSHELL_STATE", "").strip()
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    return Path(xdg or Path.home() / ".local/state") / "evoshell"


def state_dir() -> Path:
    return evoshell_state_dir() / "trading"


def ledger_dir() -> Path:
    override = os.environ.get("TRADING_DESK_LEDGER", "").strip()
    if override:
        return Path(override)
    return state_dir() / "ledger"


STATE_DIR = state_dir()


if __name__ == "__main__":
    print(ledger_dir())

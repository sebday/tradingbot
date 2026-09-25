"""Shared paths for the Kraken desk. Ledger defaults to the repo, not XDG state."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path.home() / ".local/state/omarchy/trading"


def ledger_dir() -> Path:
    override = os.environ.get("TRADING_DESK_LEDGER")
    if override:
        return Path(override)
    return ROOT / "ledger"

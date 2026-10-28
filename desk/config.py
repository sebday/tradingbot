"""Paths and config for the Grok desk. Orders stay off until desk.live is true."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def state_dir() -> Path:
    override = os.environ.get("DESK_STATE", "").strip()
    path = Path(override) if override else ROOT / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def desk_cfg(cfg: dict | None = None) -> dict:
    cfg = load_config() if cfg is None else cfg
    return cfg.get("desk") or {}


def live(cfg: dict | None = None) -> bool:
    return bool(desk_cfg(cfg).get("live"))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def next_wake_iso(now: float | None = None) -> str:
    """The next HH:02 UTC, which is when the hourly wake runs."""
    moment = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    target = moment.replace(minute=2, second=0, microsecond=0)
    if moment >= target:
        target += timedelta(hours=1)
    return target.isoformat().replace("+00:00", "Z")


def halt_path() -> Path:
    return state_dir() / "grok-halt.json"


def read_halt() -> dict | None:
    path = halt_path()
    if not path.is_file():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return row if row.get("status") == "HALTED" else None


def write_halt(reason: str) -> dict:
    row = {"ts": utc_now(), "status": "HALTED", "reason": reason, "resume": "bin/desk resume"}
    halt_path().write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    return row


def clear_halt() -> dict | None:
    was = read_halt()
    path = halt_path()
    if path.exists():
        path.unlink()
    return was


def chat_id_path() -> Path:
    return state_dir() / "grok-chat-id"


def read_chat_id() -> str | None:
    path = chat_id_path()
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def write_chat_id(session_id: str) -> None:
    chat_id_path().write_text(session_id.strip() + "\n", encoding="utf-8")

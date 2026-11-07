"""Load and validate config.json. Adds a `harness` section with safe defaults.

Legacy keys (paper, bank, kraken, entry, risk, scan, live, models, ...) are left
untouched: the old exit timer still reads them. The harness mode lives in
harness.mode and defaults to "paper".
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from . import paths
from .util import sha256_obj

HARNESS_DEFAULTS = {
    "mode": "paper",
    "seat": {
        "model": "composer-2.5",
        "timeoutSeconds": 600,
        "maxSubmissions": 3,
        "maxToolCalls": 80,
        "noteMaxChars": 2000,
        "notesKept": 20,
    },
    "entry": {
        # New entries only. Legacy positions keep the stop they were opened with.
        "minStopPct": 0.03,
        "maxStopPct": 0.08,
    },
    "paper": {
        "startingUsd": None,  # None -> bank.allocatedUsd
        "slippageBps": 10,
    },
    "exits": {
        # Harness exits on the kraken venue stay in shadow (no sells) until cutover.
        "liveKraken": False,
    },
    "sensors": {
        "maxContextAgeMinutes": 120,
        "maxUniverseAgeHours": 24,
    },
}

ALLOWED_MODES = ("paper", "live")


class ConfigError(ValueError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def with_defaults(raw: dict) -> dict:
    cfg = copy.deepcopy(raw)
    cfg["harness"] = _merge(HARNESS_DEFAULTS, raw.get("harness") or {})
    h = cfg["harness"]
    if h["paper"].get("startingUsd") is None:
        h["paper"]["startingUsd"] = float((cfg.get("bank") or {}).get("allocatedUsd") or (cfg.get("bank") or {}).get("startingUsd") or 0)
    return cfg


def validate(cfg: dict) -> list[str]:
    errs: list[str] = []
    h = cfg.get("harness") or {}
    if h.get("mode") not in ALLOWED_MODES:
        errs.append(f"harness.mode must be one of {ALLOWED_MODES}, got {h.get('mode')!r}")
    kr = cfg.get("kraken") or {}
    for k in ("passKey", "passSecret"):
        if not kr.get(k):
            errs.append(f"kraken.{k} missing")
    for section in ("bank", "entry", "risk", "scan", "live", "fee"):
        if not isinstance(cfg.get(section), dict):
            errs.append(f"{section} section missing")
    he = h.get("entry") or {}
    try:
        lo, hi = float(he.get("minStopPct")), float(he.get("maxStopPct"))
        if not (0 < lo < hi < 0.5):
            errs.append("harness.entry minStopPct/maxStopPct must satisfy 0 < min < max < 0.5")
    except (TypeError, ValueError):
        errs.append("harness.entry minStopPct/maxStopPct must be numbers")
    seat = h.get("seat") or {}
    if int(seat.get("maxSubmissions") or 0) < 1:
        errs.append("harness.seat.maxSubmissions must be >= 1")
    if int(seat.get("timeoutSeconds") or 0) < 30:
        errs.append("harness.seat.timeoutSeconds must be >= 30")
    oflags = (cfg.get("live") or {}).get("buyOflags")
    if oflags is not None and oflags != "fciq":
        errs.append(f"live.buyOflags must stay 'fciq' (got {oflags!r})")
    # Never let anything look like a secret in config.
    for k, v in kr.items():
        if isinstance(v, str) and len(v) > 60 and "/" not in v:
            errs.append(f"kraken.{k} looks like a raw secret; use a pass entry name")
    return errs


def load(path: Path | None = None) -> dict:
    path = path or paths.config_path()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = with_defaults(raw)
    errs = validate(cfg)
    if errs:
        raise ConfigError("; ".join(errs))
    return cfg


def config_hash(cfg: dict) -> str:
    return sha256_obj(cfg)


def mode(cfg: dict) -> str:
    return (cfg.get("harness") or {}).get("mode", "paper")


def trading_venue(cfg: dict) -> str:
    return "kraken" if mode(cfg) == "live" else "paper"

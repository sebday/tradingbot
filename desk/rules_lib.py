"""Exit and entry conditions. A context dict in, one word out."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .config import ROOT, parse_ts

CUSTOM_DIR = ROOT / "rules" / "custom"
LIBRARY = ("stop_pct", "trail", "time_stop", "volume_fade", "none")
ENTRY_KINDS = ("buy_limit", "buy_stop", "at_time", "custom")


def _num(ctx: dict, key: str) -> float | None:
    value = ctx.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stop_pct(ctx: dict, params: dict) -> str:
    last, entry = _num(ctx, "last"), _num(ctx, "entry")
    if last is None or not entry:
        return "hold"
    return "sell" if last <= entry * (1.0 - float(params["pct"])) else "hold"


def trail(ctx: dict, params: dict) -> str:
    last, entry, peak = _num(ctx, "last"), _num(ctx, "entry"), _num(ctx, "peak")
    if last is None or not entry or peak is None:
        return "hold"
    if (peak - entry) / entry < float(params["arm_pct"]):
        return "hold"
    return "sell" if last <= peak * (1.0 - float(params["giveback"])) else "hold"


def time_stop(ctx: dict, params: dict) -> str:
    deadline = parse_ts(params.get("deadline"))
    now = _num(ctx, "now")
    if deadline is None or now is None:
        return "hold"
    return "sell" if now >= deadline else "hold"


def volume_fade(ctx: dict, params: dict) -> str:
    ratio = _num(ctx, "volume_ratio")
    if ratio is None:
        return "hold"
    return "sell" if ratio < float(params.get("ratio", 0.2)) else "hold"


def none_rule(_ctx: dict, _params: dict) -> str:
    return "hold"


FUNCS = {
    "stop_pct": stop_pct,
    "trail": trail,
    "time_stop": time_stop,
    "volume_fade": volume_fade,
    "none": none_rule,
}


def module_file(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise SystemExit("custom module name must be a single file name")
    path = CUSTOM_DIR / (name if name.endswith(".py") else f"{name}.py")
    if path.parent != CUSTOM_DIR or not path.is_file():
        raise SystemExit(f"no custom module {path.name} in rules/custom")
    return path


def check_params(name: str, params: dict) -> None:
    if name == "stop_pct":
        pct = float(params["pct"])
        if not 0.001 <= pct <= 0.9:
            raise SystemExit("stop_pct pct must be between 0.001 and 0.9")
    elif name == "trail":
        arm, give = float(params["arm_pct"]), float(params["giveback"])
        if not 0.001 <= arm <= 2 or not 0.001 <= give <= 0.9:
            raise SystemExit("trail arm_pct and giveback are out of range")
    elif name == "time_stop":
        if parse_ts(params.get("deadline")) is None:
            raise SystemExit("time_stop needs deadline as an ISO timestamp")
    elif name == "volume_fade":
        ratio = float(params.get("ratio", 0.2))
        if ratio < 0:
            raise SystemExit("volume_fade ratio must be >= 0")
    elif name == "none":
        return
    elif name == "custom":
        module_file(str(params.get("module") or ""))
    else:
        raise SystemExit(f"unknown rule {name}")


def check_entry(kind: str, params: dict) -> None:
    if kind in {"buy_limit", "buy_stop"}:
        if params.get("price") is None:
            raise SystemExit(f"{kind} needs --price")
        if float(params["price"]) <= 0:
            raise SystemExit(f"{kind} needs --price above 0")
    elif kind == "at_time":
        if parse_ts(params.get("at")) is None:
            raise SystemExit("at_time needs --at as an ISO timestamp")
    elif kind == "custom":
        module_file(str(params.get("module") or ""))
    else:
        raise SystemExit(f"unknown entry kind {kind}")


def decide_exit(name: str, params: dict, ctx: dict) -> str | None:
    """hold or sell. None means the rule failed and the position is kept."""
    try:
        if name == "custom":
            word = _custom(params, ctx, {"hold", "sell"})
        else:
            word = FUNCS[name](ctx, params)
    except Exception:
        return None
    return word if word in {"hold", "sell"} else None


def decide_entry(kind: str, params: dict, ctx: dict) -> str | None:
    """wait or buy. None means the condition failed and the entry keeps waiting."""
    last = _num(ctx, "last")
    try:
        if kind == "buy_limit":
            if last is None:
                return "wait"
            return "buy" if last <= float(params["price"]) else "wait"
        if kind == "buy_stop":
            if last is None:
                return "wait"
            return "buy" if last >= float(params["price"]) else "wait"
        if kind == "at_time":
            when = parse_ts(params.get("at"))
            now = _num(ctx, "now")
            if when is None or now is None or now < when:
                return "wait"
            lo, hi = params.get("band_low"), params.get("band_high")
            if last is None and (lo is not None or hi is not None):
                return "wait"
            if lo is not None and last is not None and last < float(lo):
                return "wait"
            if hi is not None and last is not None and last > float(hi):
                return "wait"
            return "buy"
        if kind == "custom":
            return _custom(params, ctx, {"wait", "buy"})
    except Exception:
        return None
    return None


def _custom(params: dict, ctx: dict, allowed: set[str]) -> str | None:
    path = module_file(str(params.get("module") or ""))
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(path)],
            input=json.dumps(ctx),
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            cwd="/tmp",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").strip().splitlines()
    word = lines[-1].strip() if lines else ""
    return word if word in allowed else None


def as_dict(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    return json.loads(raw)

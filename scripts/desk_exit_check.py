#!/usr/bin/python3 -I
"""Fast exit check. Public OHLC and ticker, then kraken_execute.py sell.

Trail-armed winners close only on TRAIL_PEAK. VOLUME_6H closes only while the
peak is still under trailArmPct. A flat book does not call Kraken.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import kraken_execute as ex  # noqa: E402
import kraken_universe as ku  # noqa: E402
from desk_paths import STATE_DIR, ledger_dir  # noqa: E402

DUST_USD = 1.0
CONFIG_PATH = ROOT / "config.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def day_move_failure(pct: float | None, cfg: dict) -> str | None:
    """SCAN/VET day band. None means the move is allowed."""
    scan = cfg.get("scan") or {}
    lo = float(scan.get("dayMoveMin", 0))
    hi = float(scan.get("dayMoveMax", 0.15))
    if pct is None or pct <= lo:
        return "DAY_FLAT"
    if pct > hi:
        return "MOVE_SPENT"
    return None


def parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def peak_since_fill(ohlc: list, fill_epoch: float | None) -> tuple[float | None, bool]:
    """Max hourly high from the fill hour. Unknown if the series starts later."""
    if not ohlc or fill_epoch is None:
        return None, False
    hour = int(fill_epoch) - (int(fill_epoch) % 3600)
    oldest = int(float(ohlc[0][0]))
    if oldest > hour:
        return None, False
    highs = [float(c[2]) for c in ohlc if int(float(c[0])) >= hour]
    if not highs:
        return None, False
    return max(highs), True


def exit_rule(
    entry: float,
    peak: float | None,
    peak_known: bool,
    last: float | None,
    ratio: float | None,
    volume_known: bool,
    cfg: dict,
) -> str | None:
    """TRAIL_PEAK, VOLUME_6H, or None to hold."""
    risk = cfg.get("risk") or {}
    arm = float(risk.get("trailArmPct", 0.3))
    giveback = float(risk.get("trailGiveback", 0.2))
    floor_pct = float(risk.get("trailFloorPct", 0.1))
    vol_close = float(risk.get("volumeRatioClose", 0.2))
    armed = False
    if peak_known and peak is not None and entry > 0:
        armed = (peak - entry) / entry >= arm
        floor = max(peak * (1.0 - giveback), entry * (1.0 + floor_pct))
        if armed and last is not None and last <= floor:
            return "TRAIL_PEAK"
        if armed:
            return None
    if not volume_known or ratio is None:
        return "VOLUME_6H"
    if ratio < vol_close:
        return "VOLUME_6H"
    return None


def group_opens(rows: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for row in rows:
        pid = row.get("pair_id")
        if not pid:
            continue
        slot = by.setdefault(
            pid,
            {
                "pair_id": pid,
                "wsname": row.get("wsname") or "",
                "filled_usd": 0.0,
                "volume_base": 0.0,
                "first_ts": None,
            },
        )
        slot["filled_usd"] += float(row.get("filled") or 0)
        slot["volume_base"] += float(row.get("volume_base") or 0)
        ts = row.get("ts")
        if ts and (slot["first_ts"] is None or str(ts) < str(slot["first_ts"])):
            slot["first_ts"] = ts
    out = []
    for slot in by.values():
        qty = float(slot["volume_base"] or 0)
        slot["entry"] = (slot["filled_usd"] / qty) if qty else 0.0
        out.append(slot)
    return out


def _retry(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            value = fn()
        except Exception:
            value = None
        if value:
            return value
        if i + 1 < attempts:
            time.sleep(0.4 * (i + 1))
    return None


def measure(pair_id: str, fill_ts: str | None) -> dict:
    ohlc = _retry(lambda: ku.fetch_ohlc(pair_id, 60)) or []
    tick = _retry(lambda: ku.fetch_tickers([pair_id]).get(pair_id))
    last = None
    if tick:
        last = tick.get("last")
    stats = ku.volume_ratio_6h_24h(ohlc) if ohlc else None
    peak, known = peak_since_fill(ohlc, parse_ts(fill_ts))
    return {
        "last": float(last) if last is not None else None,
        "ratio": None if not stats else stats.get("ratio"),
        "volume_known": stats is not None and stats.get("ratio") is not None,
        "peak": peak,
        "peak_known": known,
        "volume_6h": None if not stats else stats.get("volume_6h"),
        "avg_6h": None if not stats else stats.get("avg_6h"),
    }


def append_report(row: dict) -> None:
    path = ledger_dir() / "reports.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def log_line(text: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "desk-exit.log").open("a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def sell(pos: dict, rule: str, cycle_id: str) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "kraken_execute.py"),
        "sell",
        "--pair",
        pos["pair_id"],
        "--wsname",
        pos.get("wsname") or "",
        "--cycle-id",
        cycle_id,
        "--rule",
        rule,
        "--volume-base",
        str(pos["volume_base"]),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=90)
    out = (proc.stdout or proc.stderr or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"raw": out[:400]}
    payload["exit_code"] = proc.returncode
    return payload


def check_book(cfg: dict, opens: list[dict], cycle_id: str, do_sell: bool) -> list[dict]:
    results = []
    for pos in opens:
        qty = float(pos.get("volume_base") or 0)
        if qty <= 0:
            continue
        measured = measure(pos["pair_id"], pos.get("first_ts"))
        last = measured["last"]
        if last is not None and qty * last < DUST_USD:
            results.append({"pair_id": pos["pair_id"], "action": "SKIP_DUST", "last": last})
            continue
        rule = exit_rule(
            float(pos.get("entry") or 0),
            measured["peak"],
            measured["peak_known"],
            last,
            measured["ratio"],
            measured["volume_known"],
            cfg,
        )
        row = {
            "pair_id": pos["pair_id"],
            "wsname": pos.get("wsname"),
            "action": "HOLD" if rule is None else "CLOSE",
            "rule_fired": rule,
            "entry": pos.get("entry"),
            "last": last,
            "peak": measured["peak"],
            "peak_known": measured["peak_known"],
            "ratio": measured["ratio"],
            "volume_6h": measured["volume_6h"],
            "avg_6h": measured["avg_6h"],
        }
        if rule and do_sell:
            row["sell"] = sell(pos, rule, cycle_id)
        results.append(row)
    return results


def main() -> int:
    cfg = load_config()
    venues = cfg.get("venues") or {}
    if cfg.get("paper") or venues.get("fills") != "kraken":
        print(json.dumps({"skipped": "not_live_kraken"}))
        return 0
    opens = group_opens(ex.open_fills("kraken"))
    if not opens:
        print(json.dumps({"checked": 0, "ts": utc_now()}))
        return 0
    cycle_id = utc_now()
    results = check_book(cfg, opens, cycle_id, do_sell=True)
    closes = [r for r in results if r.get("action") == "CLOSE"]
    if closes:
        append_report(
            {
                "ts": utc_now(),
                "cycle_id": cycle_id,
                "kind": "exit",
                "scan": 0,
                "vet_pass": 0,
                "fills": 0,
                "closes": closes,
                "note": "desk_exit_check",
            }
        )
        log_line(json.dumps({"ts": cycle_id, "closes": closes}, default=str))
    print(json.dumps({"checked": len(results), "results": results}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

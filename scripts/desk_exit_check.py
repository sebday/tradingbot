#!/usr/bin/python3 -I
"""Fast exit check. Public OHLC and ticker, desk_rules.decide, then kraken_execute.py sell.

STOP_LOSS fires on the ticker alone. A position that cannot be measured is
STALE: held, and alerted after risk.staleAlertMinutes, never sold blind.
A flat book does not call Kraken.
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

import desk_book  # noqa: E402
import desk_rules as rules  # noqa: E402
import kraken_universe as ku  # noqa: E402
from desk_paths import STATE_DIR, ledger_dir  # noqa: E402

DUST_USD = 1.0
CONFIG_PATH = ROOT / "config.json"
STALE_PATH = STATE_DIR / "exit-stale.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


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


def measure(pair_id: str, opened_ts: str | None) -> dict:
    tick = _retry(lambda: ku.fetch_tickers([pair_id]).get(pair_id))
    ohlc = _retry(lambda: ku.fetch_ohlc(pair_id, 60)) or []
    stats = ku.volume_ratio_6h_24h(ohlc) if ohlc else None
    peak, known = rules.peak_since_fill(ohlc, rules.parse_ts(opened_ts))
    last = (tick or {}).get("last")
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
        f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def log_line(text: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "desk-exit.log").open("a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def notify(summary: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "-a", "evo.trading", summary, body],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def load_stale() -> dict:
    try:
        return json.loads(STALE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_stale(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STALE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def track_stale(state: dict, pair_id: str, stale: bool, now: float, cfg: dict) -> bool:
    """Record a stale streak. True when this check crosses the alert threshold."""
    if not stale:
        state.pop(pair_id, None)
        return False
    row = state.setdefault(pair_id, {"since": now, "alerted": False})
    minutes = float((cfg.get("risk") or {}).get("staleAlertMinutes", 30))
    if not row["alerted"] and now - float(row["since"]) >= minutes * 60:
        row["alerted"] = True
        return True
    return False


def sell(pos: desk_book.Position, rule: str, cycle_id: str) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "kraken_execute.py"),
        "sell",
        "--pair",
        pos.pair_id,
        "--wsname",
        pos.wsname or "",
        "--cycle-id",
        cycle_id,
        "--rule",
        rule,
        "--volume-base",
        str(pos.qty),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=90)
    out = (proc.stdout or proc.stderr or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except json.JSONDecodeError:
        payload = {"raw": out[-400:]}
    payload["exit_code"] = proc.returncode
    return payload


def check_book(
    cfg: dict,
    positions: list[desk_book.Position],
    cycle_id: str,
    do_sell: bool,
    stale_state: dict | None = None,
) -> list[dict]:
    stale_state = {} if stale_state is None else stale_state
    now = time.time()
    results = []
    for pos in positions:
        if pos.qty <= 0:
            continue
        m = measure(pos.pair_id, pos.opened_ts)
        last = m["last"]
        if last is not None and pos.qty * last < DUST_USD:
            results.append({"pair_id": pos.pair_id, "action": "SKIP_DUST", "last": last})
            continue
        decision = rules.decide(pos.entry, m["peak"], m["peak_known"], last, m["ratio"], m["volume_known"], cfg)
        closing = decision in rules.CLOSE_RULES
        row = {
            "pair_id": pos.pair_id,
            "wsname": pos.wsname,
            "source": pos.source,
            "action": "CLOSE" if closing else decision,
            "rule_fired": decision if closing else None,
            "entry": pos.entry,
            "stop": rules.stop_price(pos.entry, cfg) if pos.entry else None,
            "last": last,
            "pct_vs_entry": (last / pos.entry - 1.0) if last and pos.entry else None,
            "peak": m["peak"],
            "peak_known": m["peak_known"],
            "trail_armed": rules.trail_armed(pos.entry, m["peak"], m["peak_known"], cfg),
            "ratio": m["ratio"],
            "volume_6h": m["volume_6h"],
            "avg_6h": m["avg_6h"],
        }
        if track_stale(stale_state, pos.pair_id, decision == rules.STALE, now, cfg):
            row["stale_alert"] = True
        if closing and do_sell:
            row["sell"] = sell(pos, decision, cycle_id)
        results.append(row)
    return results


def main() -> int:
    cfg = load_config()
    venues = cfg.get("venues") or {}
    if cfg.get("paper") or venues.get("fills") != "kraken":
        print(json.dumps({"skipped": "not_live_kraken"}))
        return 0
    book = desk_book.load_book("kraken")
    positions = desk_book.managed_positions(book)
    if not positions:
        save_stale({})
        print(json.dumps({"checked": 0, "ts": utc_now()}))
        return 0
    cycle_id = utc_now()
    stale_state = load_stale()
    results = check_book(cfg, positions, cycle_id, do_sell=True, stale_state=stale_state)
    save_stale(stale_state)
    closes = [r for r in results if r.get("action") == "CLOSE"]
    alerts = [r for r in results if r.get("stale_alert")]
    for r in alerts:
        notify("Trading desk: position unmeasured", f"{r['wsname'] or r['pair_id']} has had no price for a while. Held, not sold.")
    for r in closes:
        sold = r.get("sell") or {}
        if sold.get("exit_code") not in (0, None):
            notify("Trading desk: sell failed", f"{r['wsname'] or r['pair_id']} {r['rule_fired']} did not fill.")
    if closes or alerts:
        append_report(
            {
                "ts": utc_now(),
                "cycle_id": cycle_id,
                "kind": "exit",
                "scan": 0,
                "vet_pass": 0,
                "fills": 0,
                "closes": closes,
                "stale_alerts": alerts,
                "note": "desk_exit_check",
            }
        )
        log_line(json.dumps({"ts": cycle_id, "closes": closes, "stale_alerts": alerts}, default=str))
    print(json.dumps({"checked": len(results), "results": results}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

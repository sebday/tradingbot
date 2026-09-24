#!/usr/bin/env python3
"""JSON status for evo.trading Omarchy bar widget. Read-only display."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

STATE_DIR = Path.home() / ".local/state/omarchy/trading"
RUN_META = STATE_DIR / "last_run.json"


def ledger_dir() -> Path:
    import os

    return Path(os.environ.get("TRADING_DESK_LEDGER") or STATE_DIR / "ledger")


def _runtime_dir() -> Path:
    import os

    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd:
        return Path(rd)
    return Path.home() / ".cache"


def lock_path() -> Path:
    return _runtime_dir() / "omarchy-trading-desk.lock"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tail_jsonl(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def fetch_tickers(pair_ids: list[str]) -> dict[str, float]:
    if not pair_ids:
        return {}
    url = "https://api.kraken.com/0/public/Ticker?pair=" + urllib.parse.quote(",".join(pair_ids))
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return {}
    if data.get("error"):
        return {}
    result = data.get("result") or {}
    out: dict[str, float] = {}
    for pid in pair_ids:
        row = result.get(pid)
        if not row:
            continue
        try:
            last = row["c"][0]
            out[pid] = float(last)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def runner_state() -> dict:
    meta: dict = {}
    if RUN_META.is_file():
        try:
            meta = json.loads(RUN_META.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    running = bool(meta.get("running"))
    if meta.get("finished_at") is None and meta.get("started_at"):
        running = True
    stale = False
    last_ts = meta.get("finished_at") or meta.get("started_at")
    if last_ts:
        try:
            t = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60.0
            stale = age_min > 70
        except ValueError:
            stale = True
    return {
        "running": running,
        "stale": stale,
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "exit_code": meta.get("exit_code"),
        "error": meta.get("error"),
    }


def main() -> int:
    import desk_halt  # noqa: E402

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ledger = ledger_dir()
    allocated = desk_halt.allocated_usd(config)
    floor = desk_halt.halt_floor_usd(config)
    halted = desk_halt.is_halted()

    opens_payload: dict = {"opens": []}
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/kraken_execute.py"), "opens"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            opens_payload = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass

    equity = None
    usd_spot = None
    balance_err = None
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/kraken_balance.py")],
            capture_output=True,
            text=True,
            timeout=45,
            cwd=str(ROOT),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            bal = json.loads(proc.stdout)
            equity = bal.get("equivalent_usd")
            usd_spot = bal.get("usd_spot")
            halted = bool(bal.get("halted")) or halted
        else:
            balance_err = (proc.stderr or proc.stdout or "balance failed")[:200]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        balance_err = str(e)[:200]

    opens = opens_payload.get("opens") or []
    pair_ids = [str(o.get("pair_id")) for o in opens if o.get("pair_id")]
    lasts = fetch_tickers(pair_ids)

    positions = []
    for o in opens:
        pid = str(o.get("pair_id") or "")
        qty = float(o.get("volume_base") or 0)
        filled = float(o.get("filled_usd") or 0)
        entry = filled / qty if qty > 0 else 0.0
        last = lasts.get(pid)
        pnl = None
        pct = None
        if last is not None and qty > 0 and entry > 0:
            pnl = round((last - entry) * qty, 2)
            pct = round((last / entry - 1.0) * 100.0, 2)
        positions.append(
            {
                "pair_id": pid,
                "wsname": o.get("wsname"),
                "qty": qty,
                "entry": round(entry, 8) if entry else 0,
                "filled_usd": round(filled, 2),
                "last": last,
                "pnl_usd": pnl,
                "pct_vs_entry": pct,
            }
        )

    last_cycle = tail_jsonl(ledger / "reports.jsonl")
    if last_cycle and last_cycle.get("kind") == "exit":
        # prefer last full cycle for panel status
        try:
            lines = (ledger / "reports.jsonl").read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                row = json.loads(line)
                if row.get("kind") != "exit":
                    last_cycle = row
                    break
        except (OSError, json.JSONDecodeError):
            pass

    if equity is None and last_cycle:
        equity = last_cycle.get("equity_usd")
    if usd_spot is None and last_cycle:
        usd_spot = last_cycle.get("usd_spot")

    bar_text = ""
    if equity is not None:
        try:
            n = float(equity)
            whole = int(round(abs(n)))
            bar_text = f"-${whole:,}" if n < 0 else f"${whole:,}"
        except (TypeError, ValueError):
            bar_text = ""

    out = {
        "class": "ok",
        "ok": True,
        "text": bar_text,
        "tooltip": f"Desk equity {bar_text}" if bar_text else "Kraken desk",
        "equity_usd": equity,
        "usd_spot": usd_spot,
        "allocated_usd": round(allocated, 2),
        "halt_floor_usd": round(floor, 2),
        "halted": halted,
        "open_count": len(positions),
        "positions": positions,
        "last_cycle": last_cycle,
        "runner": runner_state(),
        "balance_error": balance_err,
        "desk_root": str(ROOT),
        "fetched_at": utc_now(),
    }
    if not bar_text and balance_err:
        out["ok"] = False
        out["class"] = "error"
        out["error"] = balance_err

    print(json.dumps(out, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

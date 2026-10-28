"""Grok desk rules, the trade log, and one runner pass. No Kraken, no model."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk.db import Log, connect  # noqa: E402
from desk import rules_lib  # noqa: E402
from desk.runner import run_once  # noqa: E402

OPENED = "2026-10-28T00:00:00Z"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc).timestamp()


def candles(high: float) -> list:
    base = int(datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp())
    rows = []
    for i in range(24):
        rows.append([base + i * 3600, 100, high, 99, 100, 100, 10, 1])
    return rows


class Fake:
    def __init__(self):
        self.live = False
        self.blocked = False
        self.pos = []
        self.ticks = {}
        self.candles = {}
        self.cash_usd = 1000.0
        self.buys = []
        self.sells = []
        self.cancelled = []

    def entries_blocked(self):
        return self.blocked

    def positions(self):
        return list(self.pos)

    def tickers(self, ids):
        return {i: self.ticks.get(i, {}) for i in ids}

    def ohlc(self, pair):
        return self.candles.get(pair, [])

    def cash(self):
        return self.cash_usd

    def cancel(self, txid):
        self.cancelled.append(txid)

    def buy(self, entry, ask):
        self.buys.append(entry["pair_id"])
        return {
            "ok": True, "live": False, "status": "shadow", "qty": 1.0, "price": float(ask),
            "cost_usd": float(entry["size_usd"]), "txid": None, "opened_ts": OPENED,
            "wsname": entry.get("wsname") or "",
        }

    def sell(self, pos, reason, rule):
        self.sells.append(pos.pair_id)
        return {"ok": True, "live": False, "status": "shadow", "qty": pos.qty, "price": 90.0, "proceeds_usd": 90.0}

    def place_resting(self, entry):
        raise AssertionError("shadow runner must not place a resting order")

    def query(self, txid):
        return {}


def pos(pair="SOLUSD", entry=100.0, last_ignored=None):
    return SimpleNamespace(pair_id=pair, wsname="SOL/USD", qty=1.0, entry=entry, opened_ts=OPENED)


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Log(connect(Path(self.tmp.name) / "grok.db"))
        self.addCleanup(self.log.conn.close)
        self.custom = Path(self.tmp.name) / "custom"
        self.custom.mkdir()
        self._old_custom = rules_lib.CUSTOM_DIR
        rules_lib.CUSTOM_DIR = self.custom
        self.addCleanup(self._restore_custom)

    def _restore_custom(self):
        rules_lib.CUSTOM_DIR = self._old_custom

    def test_reason_required(self):
        with self.assertRaises(SystemExit):
            self.log.add_trade({"ts": "t", "side": "buy", "pair_id": "SOLUSD", "reason": "  ", "status": "shadow", "live": 0})

    def test_stop_sells_and_records_reason(self):
        fake = Fake()
        fake.pos = [pos()]
        fake.ticks = {"SOLUSD": {"last": 90.0, "bid": 89.0, "ask": 91.0}}
        self.log.set_rule({
            "pair_id": "SOLUSD", "opened_ts": OPENED, "name": "stop_pct", "params": {"pct": 0.08},
            "reason": "eight percent under the fill", "set_at": "t",
        })
        summary = run_once(self.log, fake, NOW)
        self.assertEqual(summary["closed"], ["SOLUSD"])
        self.assertEqual(fake.sells, ["SOLUSD"])
        row = self.log.recent_trades(1)[0]
        self.assertEqual(row["reason"], "eight percent under the fill")
        self.assertEqual(row["side"], "sell")
        self.assertEqual(row["status"], "shadow")
        self.assertEqual(self.log.active_rules(), [])

    def test_no_rule_does_not_sell(self):
        fake = Fake()
        fake.pos = [pos()]
        fake.ticks = {"SOLUSD": {"last": 1.0}}
        summary = run_once(self.log, fake, NOW)
        self.assertEqual(summary["closed"], [])
        self.assertEqual(fake.sells, [])

    def test_trail_holds_above_floor(self):
        fake = Fake()
        fake.pos = [pos()]
        fake.ticks = {"SOLUSD": {"last": 106.0}}
        fake.candles = {"SOLUSD": candles(110)}
        self.log.set_rule({
            "pair_id": "SOLUSD", "opened_ts": OPENED, "name": "trail",
            "params": {"arm_pct": 0.08, "giveback": 0.05}, "reason": "give back five", "set_at": "t",
        })
        run_once(self.log, fake, NOW)
        self.assertEqual(fake.sells, [])

    def test_buy_limit_fills_in_shadow_and_attaches_stop(self):
        fake = Fake()
        fake.ticks = {"SOLUSD": {"last": 9.0, "ask": 9.1, "bid": 8.9}}
        self.log.add_entry({
            "ts": "t", "pair_id": "SOLUSD", "wsname": "SOL/USD", "size_usd": 25, "kind": "buy_limit",
            "params": {"price": 10}, "exit_rule": {"name": "stop_pct", "params": {"pct": 0.08}, "reason": "stop under the dip"},
            "reason": "buy the dip at ten", "expires_at": "2026-10-28T18:00:00Z", "status": "waiting", "live": False,
        })
        summary = run_once(self.log, fake, NOW)
        self.assertEqual(summary["bought"], ["SOLUSD"])
        self.assertEqual(fake.buys, ["SOLUSD"])
        trade = self.log.recent_trades(1)[0]
        self.assertEqual(trade["reason"], "buy the dip at ten")
        self.assertEqual(trade["side"], "buy")
        rules = self.log.active_rules()
        self.assertEqual(rules[0]["name"], "stop_pct")
        self.assertEqual(json.loads(rules[0]["params_json"])["pct"], 0.08)
        self.assertEqual(self.log.waiting_entries(), [])

    def test_buy_limit_waits_above_the_level(self):
        fake = Fake()
        fake.ticks = {"SOLUSD": {"last": 11.0, "ask": 11.1}}
        self.log.add_entry({
            "ts": "t", "pair_id": "SOLUSD", "size_usd": 25, "kind": "buy_limit",
            "params": {"price": 10}, "exit_rule": {"name": "none", "params": {}},
            "reason": "only if it dips", "expires_at": "2026-10-28T18:00:00Z", "status": "waiting", "live": False,
        })
        run_once(self.log, fake, NOW)
        self.assertEqual(fake.buys, [])
        self.assertEqual(len(self.log.waiting_entries()), 1)

    def test_expired_entry_does_not_buy(self):
        fake = Fake()
        fake.ticks = {"SOLUSD": {"last": 9.0, "ask": 9.0}}
        self.log.add_entry({
            "ts": "t", "pair_id": "SOLUSD", "size_usd": 25, "kind": "buy_limit",
            "params": {"price": 10}, "exit_rule": {"name": "none", "params": {}},
            "reason": "too late", "expires_at": "2026-10-28T01:00:00Z", "status": "waiting", "live": False,
        })
        run_once(self.log, fake, NOW)
        self.assertEqual(fake.buys, [])
        self.assertEqual(self.log.waiting_entries(), [])

    def test_halt_cancels_buys_and_still_sells(self):
        fake = Fake()
        fake.blocked = True
        fake.pos = [pos()]
        fake.ticks = {"SOLUSD": {"last": 90.0}, "XBTUSD": {"last": 1.0}}
        self.log.set_rule({
            "pair_id": "SOLUSD", "opened_ts": OPENED, "name": "stop_pct", "params": {"pct": 0.08},
            "reason": "stop stays armed", "set_at": "t",
        })
        self.log.add_entry({
            "ts": "t", "pair_id": "XBTUSD", "size_usd": 10, "kind": "at_time",
            "params": {"at": "2026-10-28T11:00:00Z"}, "exit_rule": {"name": "none", "params": {}},
            "reason": "do not buy while halted", "expires_at": "2026-10-28T18:00:00Z", "status": "waiting", "live": False,
        })
        summary = run_once(self.log, fake, NOW)
        self.assertTrue(summary["halted"])
        self.assertEqual(summary["closed"], ["SOLUSD"])
        self.assertEqual(fake.buys, [])
        self.assertEqual(self.log.waiting_entries(), [])

    def test_custom_module_can_sell(self):
        (self.custom / "always_sell.py").write_text("import sys\nsys.stdin.read()\nprint('sell')\n", encoding="utf-8")
        fake = Fake()
        fake.pos = [pos()]
        fake.ticks = {"SOLUSD": {"last": 100.0}}
        self.log.set_rule({
            "pair_id": "SOLUSD", "opened_ts": OPENED, "name": "custom", "params": {"module": "always_sell"},
            "reason": "my own rule", "set_at": "t",
        })
        run_once(self.log, fake, NOW)
        self.assertEqual(fake.sells, ["SOLUSD"])

    def test_panel_clock_uses_the_wake_not_the_old_cycle(self):
        import os
        os.environ["DESK_STATE"] = self.tmp.name
        os.environ["DESK_DB"] = str(Path(self.tmp.name) / "grok.db")
        self.addCleanup(os.environ.pop, "DESK_STATE", None)
        self.addCleanup(os.environ.pop, "DESK_DB", None)
        sys.path.insert(0, str(ROOT / "scripts"))
        import desk_status

        fresh = desk_status.runner_state()
        self.assertFalse(fresh["stale"])
        self.assertIsNone(fresh["finished_at"])
        self.assertTrue(fresh["next_wake"])
        self.log.add_wake("2026-10-28T10:00:00Z", None, "ok", "held")
        stale = desk_status.runner_state()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["finished_at"], "2026-10-28T10:00:00Z")

    def test_cli_requires_a_reason(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "desk"), "trade", "buy", "--pair", "SOLUSD", "--dollars", "10"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("reason", (proc.stderr + proc.stdout).lower())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/python3 -I
"""End-to-end loop tests: fixture market + paper broker + scripted (fake) seat. No LLM, no keys."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness import paths  # noqa: E402
from harness.book import load_book  # noqa: E402
from harness.broker.base import BrokerError  # noqa: E402
from harness.broker.kraken import KrakenBroker  # noqa: E402
from harness.broker.paper import PaperBroker  # noqa: E402
from harness.cycle import replay, run_cycle  # noqa: E402
from harness.exits import run_exits  # noqa: E402
from harness.journal import Journal  # noqa: E402
from harness.market.kraken_public import FixtureMarket  # noqa: E402
from harness.seat import runner  # noqa: E402
from harness.seat.tools import DeskTools  # noqa: E402
from harness_fixtures import Env, cfg as make_cfg, market_data  # noqa: E402

LEGACY_STATUS_KEYS = {"class", "ok", "text", "tooltip", "equity_usd", "usd_spot", "allocated_usd", "halt_floor_usd",
                      "halted", "open_count", "positions", "last_cycle", "runner", "balance_error", "desk_root",
                      "fetched_at"}
LEGACY_POSITION_KEYS = {"pair_id", "wsname", "qty", "entry", "filled_usd", "last", "pnl_usd", "pct_vs_entry", "stop_price"}


class Loop(unittest.TestCase):
    def setUp(self):
        self.env = Env().__enter__()
        self.cfg = make_cfg()
        self.j = Journal()
        self.m = FixtureMarket(market_data())
        self.b = PaperBroker(self.cfg, self.j, self.m)

    def tearDown(self):
        self.j.close()
        self.env.__exit__()

    def seat(self, script):
        return runner.ScriptedSeat(script, tools_factory=lambda: DeskTools(self.cfg, self.j, self.m))

    def cycle(self, script, **kw):
        return run_cycle(self.cfg, self.j, self.m, self.b, self.seat(script), **kw)

    def test_baseline_opens_top_rank_in_paper(self):
        r = self.cycle(runner.baseline_script)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["approved"], 1, r)
        self.assertEqual(r["baseline_pair"], "XXBTZUSD")
        self.assertTrue(r["orders"][0]["ok"], r["orders"])
        book = load_book(self.j, "paper")
        pos = book.position("XXBTZUSD")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.stop_source, "proposal")
        ask = self.m.data["tickers"]["XXBTZUSD"]["ask"]
        self.assertTrue(ask * 0.92 <= pos.stop_price <= ask * 0.97)
        rd = Path(r["run_dir"])
        for f in ("mandate.json", "sensors.json", "transcript.jsonl", "proposals.json", "gate.json", "orders.json",
                  "receipt.json", "gate_state.json"):
            self.assertTrue((rd / f).exists(), f)
        rec = json.loads((rd / "receipt.json").read_text())
        for k in ("git_sha", "config_hash", "model", "prompt_hash", "tool_calls", "duration_s", "sensors", "approved",
                  "rejected", "orders", "errors"):
            self.assertIn(k, rec)
        order = self.j.q("SELECT * FROM orders")[0]
        self.assertEqual(order["oflags"], "fciq")
        self.assertEqual(order["timeinforce"], "IOC")
        self.assertEqual(replay(self.cfg, r["cycle_id"])["same"], True)
        # second cycle without exits running -> observe-only (sensor), then with exits -> next rank opens
        r_obs = self.cycle(runner.baseline_script)
        self.assertEqual(r_obs["observe_only"], ["exits_running"])
        run_exits(self.cfg, self.j, self.m, self.b, "paper", do_sell=True)
        r2 = self.cycle(runner.baseline_script)
        self.assertEqual(len(load_book(self.j, "paper").positions), 2)  # next rank (ETH) is allowed
        self.assertEqual(r2["status"], "ok")

    def test_hold_trades_nothing(self):
        r = self.cycle(runner.hold_script)
        self.assertEqual((r["status"], r["approved"], r["orders"]), ("ok", 0, []))

    def test_red_sensor_is_observe_only_and_skips_seat(self):
        self.m.data["tickers"] = {}
        called = []
        r = self.cycle(lambda call, m: called.append(1))
        self.assertEqual(r["status"], "observe_only")
        self.assertIn("ticker_fresh", r["observe_only"])
        self.assertEqual(called, [])
        self.assertTrue((Path(r["run_dir"]) / "receipt.json").exists())

    def test_halt_blocks_opens(self):
        self.j.set_halt({"status": "HALTED", "reason": "test"})
        r = self.cycle(runner.baseline_script)
        self.assertEqual(r["status"], "observe_only")
        self.assertEqual(load_book(self.j, "paper").positions, {})

    def test_bad_evidence_and_submission_limit(self):
        def script(call, m):
            p = {"action": "open", "pair_id": "SOLUSD", "size_usd": 20, "stop_price": 140.0, "thesis": "x",
                 "invalidation": "y", "evidence": ["made-up"]}
            for _ in range(3):
                res = call("submit_proposals", {"cycle_id": m.cycle_id, "proposals": [p]})
                self.assertFalse(res["data"]["valid"])
            res = call("submit_proposals", {"cycle_id": m.cycle_id, "proposals": [p]})
            self.assertEqual(res["error"]["code"], "SUBMISSION_LIMIT")
        r = self.cycle(script)
        self.assertEqual(r["status"], "no_submission")
        self.assertEqual(r["orders"], [])

    def test_gate_rejects_oversize_even_if_schema_ok(self):
        def script(call, m):
            u = call("get_universe")
            res = call("submit_proposals", {"cycle_id": m.cycle_id, "proposals": [
                {"action": "open", "pair_id": "SOLUSD", "size_usd": 400, "stop_price": 145.0, "thesis": "x",
                 "invalidation": "y", "evidence": [u["tool_call_id"]]}]})
            self.assertFalse(res["data"]["dry_run_gate"][0]["would_approve"])
        r = self.cycle(script)
        self.assertEqual(r["approved"], 0)
        self.assertIn("TICKET_CAP", r["reject_reasons"])
        self.assertEqual(load_book(self.j, "paper").positions, {})

    def test_timeout_means_no_trade(self):
        class TimeoutSeat:
            def __init__(s, inner):
                s.inner = inner

            def run(s, mandate, run_dir):
                s.inner.run(mandate, run_dir)
                return {"ok": False, "timed_out": True, "model": "scripted"}
        r = run_cycle(self.cfg, self.j, self.m, self.b, TimeoutSeat(self.seat(runner.baseline_script)))
        self.assertEqual(r["status"], "seat_timeout")
        self.assertEqual(load_book(self.j, "paper").positions, {})

    def test_tools_reject_pairs_outside_universe_and_after_deadline(self):
        def script(call, m):
            self.assertEqual(call("get_ticker", {"pair_ids": ["DOGEUSD"]})["error"]["code"], "PAIR_NOT_ALLOWED")
            self.assertTrue(call("get_candles", {"pair_id": "SOLUSD", "interval": 60, "days": 1})["ok"])
            self.assertEqual(call("write_note", {"text": "x" * 5000})["error"]["code"], "NOTE_TOO_LONG")
            self.assertTrue(call("write_note", {"text": "lesson"})["ok"])
        self.cycle(script)
        self.assertEqual(self.j.notes()[0]["text"], "lesson")

    def test_paper_exit_stop_fires(self):
        self.cycle(runner.baseline_script)
        pos = load_book(self.j, "paper").position("XXBTZUSD")
        t = self.m.data["tickers"]["XXBTZUSD"]
        low = pos.stop_price * 0.99
        t.update(last=low, bid=low * 0.999, ask=low * 1.001)
        cash_before = self.b.balance()["usd_spot"]
        r = run_exits(self.cfg, self.j, self.m, self.b, "paper", do_sell=True)
        self.assertEqual(r["closes"][0]["rule_fired"], "STOP_LOSS")
        self.assertIsNone(load_book(self.j, "paper").position("XXBTZUSD"))
        self.assertGreater(self.b.balance()["usd_spot"], cash_before)

    def test_exits_shadow_does_not_sell(self):
        self.j.add_fill({"ts": "2026-10-27T10:00:00Z", "venue": "kraken", "pair_id": "SOLUSD", "qty": 0.2, "cost_usd": 30,
                         "dedupe": "k1"})
        self.j.set_stop("kraken", "SOLUSD", "2026-10-27T10:00:00Z", 150 * 0.92 * 1.5, "legacy_import")
        kb = KrakenBroker(self.cfg)  # orders disabled
        r = run_exits(self.cfg, self.j, self.m, kb, "kraken", do_sell=False)
        self.assertEqual(r["closes"][0]["rule_fired"], "STOP_LOSS")
        self.assertNotIn("sell", r["closes"][0])
        self.assertIsNotNone(load_book(self.j, "kraken").position("SOLUSD"))

    def test_kraken_broker_refuses_orders_by_default(self):
        kb = KrakenBroker(self.cfg)
        with self.assertRaises(BrokerError):
            kb.buy_limit_ioc({"pair_id": "SOLUSD"}, 10, 100, "c")
        with self.assertRaises(BrokerError):
            kb.sell_market({"pair_id": "SOLUSD"}, 1, "c")

    def test_cycle_refuses_kraken_in_paper_mode(self):
        r = run_cycle(self.cfg, self.j, self.m, KrakenBroker(self.cfg), self.seat(runner.hold_script))
        self.assertEqual(r["status"], "error")

    def test_status_keeps_legacy_shape(self):
        from harness.status import build_status
        self.j.add_fill({"ts": "2026-10-27T10:00:00Z", "venue": "kraken", "pair_id": "SOLUSD", "wsname": "SOL/USD",
                         "qty": 0.2, "cost_usd": 30, "dedupe": "k1"})
        self.j.set_stop("kraken", "SOLUSD", "2026-10-27T10:00:00Z", 138.0, "legacy_import")
        s = build_status(self.cfg, self.j, self.m, kraken_broker=None, paper_broker=self.b, sync_legacy=False)
        self.assertTrue(LEGACY_STATUS_KEYS <= set(s), LEGACY_STATUS_KEYS - set(s))
        self.assertTrue(LEGACY_POSITION_KEYS <= set(s["positions"][0]))
        self.assertEqual(s["positions"][0]["stop_price"], 138.0)
        for k in ("harness", "last_receipt", "gate"):
            self.assertIn(k, s)
        json.dumps(s)

    def test_mcp_stdio_subprocess(self):
        fx = self.env.dir / "market.json"
        fx.write_text(json.dumps(market_data()))
        cfgp = self.env.dir / "config.json"
        raw = json.loads(json.dumps(self.cfg))
        cfgp.write_text(json.dumps(raw))
        from harness.contract import Mandate
        from harness.util import iso, write_json
        import time as _t
        m = Mandate(cycle_id=iso(_t.time()), mode="paper", venue="paper", created_at=iso(_t.time()),
                    deadline=iso(_t.time() + 300), allowed_actions=["hold"], budget_usd=0, max_fills=1, max_submissions=3,
                    universe=[], universe_hash="x", portfolio={}, observe_only=True)
        mp = self.env.dir / "mandate.json"
        write_json(mp, m.to_dict())
        write_json(paths.active_cycle_path(), {"cycle_id": m.cycle_id, "mandate_path": str(mp)})
        env = dict(os.environ, DESK_FAKE_MARKET=str(fx), DESK_CONFIG=str(cfgp))
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                                                                                "capabilities": {}, "clientInfo": {"name": "t"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_mandate", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
                {"jsonrpc": "2.0", "id": 5, "method": "bogus"}]
        proc = subprocess.run([str(ROOT / "bin" / "desk"), "mcp-serve"], input="\n".join(json.dumps(x) for x in msgs) + "\n",
                              capture_output=True, text=True, env=env, timeout=30)
        lines = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual([x["id"] for x in lines], [1, 2, 3, 4, 5], proc.stderr)
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "desk")
        names = {t["name"] for t in lines[1]["result"]["tools"]}
        self.assertEqual(names, {"get_mandate", "get_portfolio", "get_universe", "get_ticker", "get_candles", "get_context",
                                 "get_recent_grades", "get_notes", "submit_proposals", "write_note"})
        body = json.loads(lines[2]["result"]["content"][0]["text"])
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["observe_only"])
        self.assertIn("error", lines[3])
        self.assertEqual(lines[4]["error"]["code"], -32601)
        self.assertEqual(Journal().tool_call_count(m.cycle_id), 1)


class Doctor(unittest.TestCase):
    def test_side_effects_gated(self):
        from harness.doctor import check_side_effects
        ok, ev = check_side_effects()
        self.assertTrue(ok, ev)

    def test_rules_dual_written(self):
        from harness.doctor import check_rules_dual_written
        ok, ev = check_rules_dual_written()
        self.assertTrue(ok, ev)

    def test_seat_isolation_files(self):
        from harness.sensors import seat_isolation
        ok, ev = seat_isolation()
        self.assertTrue(ok, ev)


if __name__ == "__main__":
    unittest.main()

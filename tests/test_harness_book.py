#!/usr/bin/python3 -I
"""Book, journal, and import-legacy tests (stop preservation is the critical one)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness import policy  # noqa: E402
from harness.book import build_book, load_book, reconcile  # noqa: E402
from harness.journal import Journal  # noqa: E402
from harness.legacy import import_legacy  # noqa: E402
from harness_fixtures import Env, cfg as make_cfg  # noqa: E402

LEGACY_FILLS = [
    {"ts": "2026-10-20T10:00:10Z", "cycle_id": "c1", "pair_id": "SOLUSD", "wsname": "SOL/USD", "filled": 30.0,
     "fill_price": 150.0, "venue": "kraken", "txid": "T1", "volume_base": 0.2, "fee_usd": 0.12},
    {"ts": "2026-10-22T10:00:10Z", "cycle_id": "c2", "pair_id": "SOLUSD", "wsname": "SOL/USD", "filled": 20.0,
     "fill_price": 160.0, "venue": "kraken", "txid": "T2", "volume_base": 0.125, "fee_usd": 0.08},
    {"ts": "2026-10-23T10:00:10Z", "cycle_id": "c3", "pair_id": "XETHZUSD", "wsname": "ETH/USD", "filled": 28.5,
     "fill_price": 2368.1, "venue": "kraken", "txid": "T3", "volume_base": 0.01203329, "fee_usd": 0.17},
    {"ts": "2026-10-24T10:00:10Z", "cycle_id": "c4", "pair_id": "DASHUSD", "wsname": "DASH/USD", "filled": 27.0,
     "fill_price": 53.0, "venue": "kraken", "txid": "T4", "volume_base": 0.5094, "fee_usd": 0.1},
    {"ts": "2026-10-05T10:00:10Z", "cycle_id": "p0", "pair_id": "SOLUSD", "wsname": "SOL/USD", "filled": 10.0,
     "volume_base": 0.1},  # old paper-era row, no venue -> legacy-paper
]
LEGACY_CLOSES = [
    {"ts": "2026-10-21T08:40:01Z", "cycle_id": "x1", "pair_id": "SOLUSD", "action": "CLOSE", "rule_fired": "TRAIL_PEAK",
     "venue": "kraken", "txid": "S1", "filled": 33.0, "volume_base": 0.2, "status": "closed"},
    {"ts": "2026-10-25T08:40:01Z", "cycle_id": "x2", "pair_id": "DASHUSD", "action": "SELL_FAILED", "venue": "kraken",
     "txid": "S2", "filled": 0, "volume_base": 0.5094, "status": "canceled"},
    {"ts": "2026-10-26T08:40:01Z", "cycle_id": "x3", "pair_id": "DASHUSD", "action": "HOLD", "venue": "kraken"},
]


class BookTests(unittest.TestCase):
    def test_episode_resets_after_close(self):
        fills = [{"ts": "a1", "pair_id": "P", "qty": 1, "cost_usd": 10}, {"ts": "a3", "pair_id": "P", "qty": 2, "cost_usd": 30}]
        closes = [{"ts": "a2", "pair_id": "P", "proceeds_usd": 12, "status": "closed"}]
        b = build_book("paper", fills, closes)
        self.assertEqual(b.position("P").qty, 2)
        self.assertEqual(b.position("P").entry, 15)
        self.assertEqual(b.position("P").opened_ts, "a3")
        self.assertEqual(len(b.closed), 1)
        self.assertEqual(b.last_close_ts["P"], "a2")

    def test_failed_sell_does_not_close(self):
        fills = [{"ts": "a1", "pair_id": "P", "qty": 1, "cost_usd": 10}]
        b = build_book("paper", fills, [{"ts": "a2", "pair_id": "P", "status": "canceled"}])
        self.assertIsNotNone(b.position("P"))

    def test_reconcile(self):
        fills = [{"ts": "a1", "pair_id": "SOLUSD", "qty": 1, "cost_usd": 10}]
        b = build_book("kraken", fills, [])
        pairs = [{"pair_id": "SOLUSD", "base": "SOL", "wsname": "SOL/USD"}, {"pair_id": "XETHZUSD", "base": "XETH"}]
        r = reconcile(b, [{"asset": "SOL", "amount": 1.0}], pairs)
        self.assertTrue(r["clean"])
        r = reconcile(b, [{"asset": "SOL", "amount": 0.5}, {"asset": "XETH", "amount": 1.0}], pairs, {"XETHZUSD": 2000})
        self.assertFalse(r["clean"])
        self.assertEqual(r["missing"][0]["pair_id"], "SOLUSD")
        self.assertEqual(r["untracked"][0]["pair_id"], "XETHZUSD")


class ImportLegacy(unittest.TestCase):
    def test_import_preserves_stops_and_is_idempotent(self):
        with Env() as env:
            env.write_ledger("fills.jsonl", LEGACY_FILLS)
            env.write_ledger("closes.jsonl", LEGACY_CLOSES)
            c = make_cfg()
            j = Journal()
            r = import_legacy(j, c)
            self.assertEqual(r["fills_inserted"], 5)
            self.assertEqual(r["closes_inserted"], 1)  # SELL_FAILED and HOLD skipped
            b = load_book(j, "kraken")
            self.assertEqual(sorted(b.positions), ["DASHUSD", "SOLUSD", "XETHZUSD"])
            sol = b.position("SOLUSD")
            self.assertEqual(sol.opened_ts, "2026-10-22T10:00:10Z")  # new episode after the close
            self.assertAlmostEqual(sol.stop_price, 160.0 * 0.92)
            self.assertEqual(sol.stop_source, "legacy_import")
            eth = b.position("XETHZUSD")
            self.assertAlmostEqual(eth.stop_price, (28.5 / 0.01203329) * 0.92)
            # paper-era rows are segregated
            self.assertEqual(load_book(j, "paper").positions, {})
            self.assertIn("SOLUSD", load_book(j, "legacy-paper").positions)

            # A config change after import must NOT move a live stop.
            c2 = make_cfg()
            c2["risk"]["stopLossPct"] = 0.02
            c2["harness"]["entry"]["maxStopPct"] = 0.04
            r2 = import_legacy(j, c2)
            self.assertEqual(r2["fills_inserted"], 0)
            self.assertEqual(r2["closes_inserted"], 0)
            self.assertEqual(r2["stops_frozen"], [])
            self.assertAlmostEqual(load_book(j, "kraken").position("SOLUSD").stop_price, 160.0 * 0.92)

    def test_import_matches_legacy_desk_book(self):
        if not (ROOT / "scripts" / "desk_book.py").exists():
            self.skipTest("scripts/desk_book.py not present")
        with Env() as env:
            env.write_ledger("fills.jsonl", LEGACY_FILLS)
            env.write_ledger("closes.jsonl", LEGACY_CLOSES)
            r = import_legacy(Journal(), make_cfg())
            self.assertEqual(r["legacy_crosscheck"], "ok", r["mismatches"])

    def test_dry_run_writes_nothing(self):
        with Env() as env:
            env.write_ledger("fills.jsonl", LEGACY_FILLS)
            env.write_ledger("closes.jsonl", LEGACY_CLOSES)
            j = Journal()
            r = import_legacy(j, make_cfg(), dry_run=True)
            self.assertEqual(len(r["open_positions"]), 3)
            self.assertEqual(j.fills("kraken"), [])

    def test_legacy_stop_formula_matches_desk_rules(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import desk_rules  # type: ignore
        except ImportError:
            self.skipTest("scripts/desk_rules.py not present")
        c = make_cfg()
        for entry in (0.6873898697950542, 52.99, 2368.1):
            self.assertEqual(policy.legacy_stop_price(entry, c), desk_rules.stop_price(entry, c))


class JournalTests(unittest.TestCase):
    def test_tables_and_dedupe(self):
        with Env():
            j = Journal()
            names = {r["name"] for r in j.q("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("runs", "tool_calls", "proposals", "gate_decisions", "orders", "fills", "closes", "grades", "notes",
                      "incidents"):
                self.assertIn(t, names)
            row = {"ts": "t", "venue": "paper", "pair_id": "P", "qty": 1, "cost_usd": 1, "dedupe": "d1"}
            self.assertIsNotNone(j.add_fill(row))
            self.assertIsNone(j.add_fill(row))

    def test_notes_capped(self):
        with Env():
            j = Journal()
            for i in range(30):
                j.add_note("c", f"n{i}", keep=5)
            self.assertEqual(len(j.notes(50)), 5)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/python3 -I
"""Policy unit tests. One test_rule_<id> per policy.RULES entry (doctor checks)."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness import policy  # noqa: E402
from harness.book import Book, ClosedEpisode, Position  # noqa: E402
from harness.util import iso  # noqa: E402
from harness_fixtures import cfg as make_cfg  # noqa: E402

NOW = 1_800_000_000.0


def book(positions=(), closed=(), last_close=None):
    pos = {p.pair_id: p for p in positions}
    return Book(venue="paper", positions=pos, closed=list(closed), last_close_ts=dict(last_close or {}))


def held(pid, qty=1.0, cost=100.0, opened=None, stop=None):
    return Position(pair_id=pid, wsname=pid, venue="paper", qty=qty, cost_usd=cost, opened_ts=opened or iso(NOW - 10 * 86400),
                    stop_price=stop)


def state(cfg=None, bk=None, equity=500.0, spot=300.0, halted=False, allowed=None, universe=None, tickers=None,
          stops=None, budget=1000.0, max_fills=1):
    cfg = cfg or make_cfg()
    universe = universe if universe is not None else {
        "SOLUSD": {"pair_id": "SOLUSD", "ask": 100.0, "bid": 99.9, "min_ticket_usd": 2.0},
        "XETHZUSD": {"pair_id": "XETHZUSD", "ask": 2000.0, "bid": 1999.0, "min_ticket_usd": 1.0},
    }
    tickers = tickers if tickers is not None else {pid: {"ask": r["ask"], "bid": r["bid"], "last": r["ask"]}
                                                   for pid, r in universe.items()}
    return policy.GateState(cfg=cfg, book=bk or book(), equity_usd=equity, usd_spot=spot, now_epoch=NOW, halted=halted,
                            allowed_actions=allowed or list(policy.ACTIONS), universe=universe, tickers=tickers,
                            stops=stops or {}, budget_usd=budget, max_fills=max_fills)


def opn(pid="SOLUSD", size=25.0, stop=95.0, **kw):
    return {"action": "open", "pair_id": pid, "size_usd": size, "stop_price": stop, "thesis": "t", "invalidation": "i",
            "evidence": ["x"], **kw}


def reasons(p, st):
    return policy.gate_one(p, st)["reasons"]


class Rules(unittest.TestCase):
    def test_valid_open_approved(self):
        d = policy.gate_one(opn(), state())
        self.assertTrue(d["approved"], d)
        self.assertAlmostEqual(d["limit_price"], 100.0 * 1.004)

    def test_rule_halted(self):
        self.assertIn("HALTED", reasons(opn(), state(halted=True)))

    def test_rule_action_not_allowed(self):
        self.assertIn("ACTION_NOT_ALLOWED", reasons(opn(), state(allowed=["hold"])))

    def test_rule_not_in_universe(self):
        self.assertIn("NOT_IN_UNIVERSE", reasons(opn("DOGEUSD"), state(tickers={"DOGEUSD": {"ask": 1, "bid": 1}})))

    def test_rule_already_held(self):
        self.assertIn("ALREADY_HELD", reasons(opn(), state(bk=book([held("SOLUSD")]))))

    def test_rule_reentry_cooldown(self):
        bk = book(last_close={"SOLUSD": iso(NOW - 3600)})
        self.assertIn("REENTRY_COOLDOWN", reasons(opn(), state(bk=bk)))
        bk = book(last_close={"SOLUSD": iso(NOW - 25 * 3600)})
        self.assertNotIn("REENTRY_COOLDOWN", reasons(opn(), state(bk=bk)))

    def test_rule_max_open_books(self):
        bk = book([held(f"P{i}") for i in range(7)])
        self.assertIn("MAX_OPEN_BOOKS", reasons(opn(), state(bk=bk)))

    def test_rule_max_new_per_day(self):
        bk = book([held(f"P{i}", opened=iso(NOW - 3600)) for i in range(3)])
        self.assertIn("MAX_NEW_PER_DAY", reasons(opn(), state(bk=bk)))

    def test_rule_max_fills_per_cycle(self):
        st = state(max_fills=1)
        out = policy.gate([opn("SOLUSD", stop=95.0), opn("XETHZUSD", stop=1900.0)], st)
        self.assertTrue(out[0]["approved"])
        self.assertIn("MAX_FILLS_PER_CYCLE", out[1]["reasons"])

    def test_rule_stop_required(self):
        p = opn()
        p.pop("stop_price")
        self.assertIn("STOP_REQUIRED", reasons(p, state()))

    def test_rule_stop_too_tight(self):
        # minStopPct 3% under ask 100 -> highest allowed stop 97
        self.assertIn("STOP_TOO_TIGHT", reasons(opn(stop=98.0), state()))
        self.assertNotIn("STOP_TOO_TIGHT", reasons(opn(stop=97.0), state()))

    def test_rule_stop_too_wide(self):
        # maxStopPct 8% under ask 100 -> lowest allowed stop 92
        self.assertIn("STOP_TOO_WIDE", reasons(opn(stop=91.0), state()))
        self.assertNotIn("STOP_TOO_WIDE", reasons(opn(stop=92.0), state()))

    def test_rule_stop_loosen(self):
        bk = book([held("SOLUSD", stop=90.0)])
        st = state(bk=bk, stops={"SOLUSD": 90.0})
        p = {"action": "tighten_stop", "pair_id": "SOLUSD", "stop_price": 89.0, "thesis": "t", "evidence": ["x"]}
        self.assertIn("STOP_LOOSEN", reasons(p, st))
        p["stop_price"] = 90.0
        self.assertIn("STOP_LOOSEN", reasons(p, st))
        p["stop_price"] = 95.0
        self.assertTrue(policy.gate_one(p, state(bk=bk, stops={"SOLUSD": 90.0}))["approved"])

    def test_rule_ticket_cap(self):
        # 6% of 500 equity = 30
        self.assertIn("TICKET_CAP", reasons(opn(size=31.0), state()))
        self.assertNotIn("TICKET_CAP", reasons(opn(size=30.0), state()))

    def test_rule_exposure_cap(self):
        # 80% of 500 = 400 room; invested 450 -> no room
        self.assertIn("EXPOSURE_CAP", reasons(opn(size=20.0), state(equity=500.0, spot=50.0)))

    def test_rule_cash(self):
        st = state(equity=500.0, spot=5.0)
        st.invested_usd = 100.0
        self.assertIn("CASH", reasons(opn(size=20.0), st))

    def test_rule_ticket_floor(self):
        self.assertIn("TICKET_FLOOR", reasons(opn(size=5.0), state()))
        uni = {"SOLUSD": {"pair_id": "SOLUSD", "ask": 100.0, "bid": 99.9, "min_ticket_usd": 40.0}}
        self.assertIn("TICKET_FLOOR", reasons(opn(size=25.0), state(universe=uni, equity=2000.0, spot=1500.0)))

    def test_rule_budget(self):
        self.assertIn("BUDGET", reasons(opn(size=25.0), state(budget=20.0)))

    def test_rule_not_held(self):
        p = {"action": "close", "pair_id": "SOLUSD", "thesis": "t", "evidence": ["x"]}
        self.assertIn("NOT_HELD", reasons(p, state()))
        p = {"action": "tighten_stop", "pair_id": "SOLUSD", "stop_price": 1.0, "thesis": "t", "evidence": ["x"]}
        self.assertIn("NOT_HELD", reasons(p, state()))

    def test_rule_no_price(self):
        self.assertIn("NO_PRICE", reasons(opn(), state(tickers={})))

    def test_rule_duplicate_pair(self):
        out = policy.gate([opn(), {"action": "close", "pair_id": "SOLUSD", "thesis": "t", "evidence": ["x"]}], state(max_fills=3))
        self.assertIn("DUPLICATE_PAIR", out[1]["reasons"])

    def test_tighten_over_bid_rejected(self):
        bk = book([held("SOLUSD", stop=90.0)])
        p = {"action": "tighten_stop", "pair_id": "SOLUSD", "stop_price": 100.0, "thesis": "t", "evidence": ["x"]}
        self.assertIn("STOP_TOO_TIGHT", reasons(p, state(bk=bk, stops={"SOLUSD": 90.0})))

    def test_hold_always_ok(self):
        self.assertTrue(policy.gate_one({"action": "hold", "thesis": "t"}, state(halted=True))["approved"])

    def test_close_allowed_while_halted(self):
        bk = book([held("SOLUSD")])
        p = {"action": "close", "pair_id": "SOLUSD", "thesis": "t", "evidence": ["x"]}
        self.assertTrue(policy.gate_one(p, state(bk=bk, halted=True))["approved"])

    def test_every_rule_has_a_test(self):
        names = {n for n in dir(self) if n.startswith("test_rule_")}
        for r in policy.RULES:
            self.assertIn(f"test_rule_{r.lower()}", names)

    def test_gate_is_pure_no_io_imports(self):
        import ast
        tree = ast.parse((ROOT / "harness" / "policy.py").read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        for bad in ("os", "subprocess", "urllib", "urllib.request", "socket", "sqlite3", "time", "pathlib"):
            self.assertNotIn(bad, mods)


class Exits(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_stop_uses_stored_stop_not_entry(self):
        # entry 100, legacy stop would be 92; stored stop 95 must fire at 94.9
        self.assertEqual(policy.decide(100, 95.0, None, False, 94.9, 1.0, True, self.cfg), policy.STOP_LOSS)
        self.assertEqual(policy.decide(100, 95.0, None, False, 95.1, 1.0, True, self.cfg), policy.HOLD)

    def test_missing_stop_is_stale_never_sold(self):
        self.assertEqual(policy.decide(100, None, None, False, 50.0, 1.0, True, self.cfg), policy.STALE)

    def test_matches_legacy_rule_with_legacy_stop(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import desk_rules  # type: ignore
        except ImportError:
            self.skipTest("scripts/desk_rules.py not present")
        cases = [(100, 105, True, 91.0, 1.0, True), (100, 112, True, 105.0, 1.0, True), (100, 112, True, 111.0, 1.0, True),
                 (100, None, False, 99.0, 0.1, True), (100, None, False, 99.0, None, False), (100, None, False, None, 1, True)]
        for entry, peak, known, last, ratio, vk in cases:
            stop = policy.legacy_stop_price(entry, self.cfg)
            self.assertEqual(policy.decide(entry, stop, peak, known, last, ratio, vk, self.cfg),
                             desk_rules.decide(entry, peak, known, last, ratio, vk, self.cfg))

    def test_trail(self):
        # armed at +8%, giveback 5%, floor +3%
        self.assertTrue(policy.trail_armed(100, 108, True, self.cfg))
        self.assertAlmostEqual(policy.trail_floor(100, 120, self.cfg), 114.0)
        self.assertEqual(policy.decide(100, 92, 120, True, 113.9, 1.0, True, self.cfg), policy.TRAIL_PEAK)

    def test_volume(self):
        self.assertEqual(policy.decide(100, 92, None, False, 101, 0.1, True, self.cfg), policy.VOLUME_6H)

    def test_peak_since_fill(self):
        t0 = 1_700_000_000
        hour = t0 - t0 % 3600
        bars = [[hour - 3600, 0, 50, 0, 0, 0, 0], [hour, 0, 10, 0, 0, 0, 0], [hour + 3600, 0, 12, 0, 0, 0, 0]]
        self.assertEqual(policy.peak_since_fill(bars, t0), (12.0, True))
        self.assertEqual(policy.peak_since_fill(bars[1:], t0 - 7200), (None, False))


class Sizing(unittest.TestCase):
    def test_ticket_port(self):
        c = make_cfg()
        self.assertEqual(policy.ticket_usd(500, 300, 200, 1, c), (30.0, None))
        self.assertEqual(policy.ticket_usd(500, 300, 200, 1, c, partial=True), (15.0, None))
        self.assertEqual(policy.ticket_usd(500, 300, 400, 1, c)[1], "EXPOSURE_CAP")

    def test_halt_floor(self):
        c = make_cfg()
        self.assertAlmostEqual(policy.halt_floor_usd(c), 293.74)
        self.assertTrue(policy.should_trip(293.74, c))
        self.assertFalse(policy.should_trip(300, c))

    def test_rank_matches_legacy_order(self):
        from harness_fixtures import market_data
        md = market_data()
        r = policy.rank_shortlist(md["universe"]["pairs"], md["tickers"], book(), make_cfg(), time.time())
        self.assertEqual([c["pair_id"] for c in r["candidates"]], ["XXBTZUSD", "XETHZUSD", "SOLUSD"])
        self.assertEqual(r["gates"].get("EXCLUDED_BASE"), 1)
        self.assertEqual(r["gates"].get("VOLUME"), 1)


if __name__ == "__main__":
    unittest.main()

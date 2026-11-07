#!/usr/bin/python3 -I
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.config import ConfigError, validate, with_defaults  # noqa: E402
from harness.contract import Mandate, universe_hash, validate_submission  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from harness_fixtures import BASE_CONFIG  # noqa: E402


def mandate():
    return Mandate(cycle_id="2026-10-28T18:00:00Z", mode="paper", venue="paper", created_at="x", deadline="y",
                   allowed_actions=["open", "close", "tighten_stop", "hold"], budget_usd=100, max_fills=1,
                   max_submissions=3, universe=[{"pair_id": "SOLUSD"}], universe_hash=universe_hash([{"pair_id": "SOLUSD"}]),
                   portfolio={}, observe_only=False)


GOOD = {"action": "open", "pair_id": "SOLUSD", "size_usd": 20, "stop_price": 95.0, "thesis": "why",
        "invalidation": "below 95", "evidence": ["t1"]}


class Contract(unittest.TestCase):
    def check(self, props, ids=("t1",), cycle="2026-10-28T18:00:00Z"):
        return validate_submission({"cycle_id": cycle, "proposals": props}, mandate(), set(ids))

    def test_valid(self):
        _, per, top = self.check([GOOD, {"action": "hold", "thesis": "flat"}])
        self.assertEqual(top, [])
        self.assertEqual(per, [[], []])

    def test_wrong_cycle(self):
        _, _, top = self.check([GOOD], cycle="other")
        self.assertTrue(top)

    def test_unknown_evidence_rejected(self):
        _, per, _ = self.check([{**GOOD, "evidence": ["t9"]}])
        self.assertTrue(any("unknown evidence" in e for e in per[0]))

    def test_open_requires_fields(self):
        for k in ("size_usd", "stop_price", "invalidation", "thesis", "evidence"):
            p = dict(GOOD)
            p.pop(k)
            _, per, _ = self.check([p])
            self.assertTrue(per[0], k)

    def test_bad_types(self):
        _, per, _ = self.check([{**GOOD, "size_usd": "20"}, {**GOOD, "stop_price": True}, {**GOOD, "action": "buy"}])
        self.assertTrue(all(per))

    def test_unknown_fields(self):
        _, per, _ = self.check([{**GOOD, "leverage": 5}])
        self.assertTrue(per[0])

    def test_mandate_roundtrip(self):
        m = mandate()
        self.assertEqual(Mandate.from_dict(m.to_dict()), m)


class Config(unittest.TestCase):
    def test_defaults_paper(self):
        c = with_defaults(BASE_CONFIG)
        self.assertEqual(c["harness"]["mode"], "paper")
        self.assertEqual(c["harness"]["paper"]["startingUsd"], 587.48)
        self.assertEqual(validate(c), [])
        self.assertFalse(c["paper"])  # legacy key untouched

    def test_oflags_locked(self):
        c = with_defaults({**BASE_CONFIG, "live": {"maxFillsPerCycle": 1, "buyOflags": "post"}})
        self.assertTrue(any("buyOflags" in e for e in validate(c)))

    def test_bad_mode(self):
        c = with_defaults({**BASE_CONFIG, "harness": {"mode": "yolo"}})
        self.assertTrue(validate(c))

    def test_real_config_loads(self):
        from harness import config
        p = ROOT / "config.json"
        if not p.exists():
            self.skipTest("no config.json")
        try:
            c = config.load(p)
        except ConfigError as e:
            self.fail(str(e))
        self.assertEqual(c["harness"]["mode"], "paper")


if __name__ == "__main__":
    unittest.main()

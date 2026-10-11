#!/usr/bin/python3 -I
"""Daily candle shaping. No network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pair_candles  # noqa: E402


class PairCandlesTest(unittest.TestCase):
    def test_keeps_the_latest_days(self):
        rows = [
            [1_700_000_000 + i * 86400, 1, 3, 0.5, 2, 0, 0, 0]
            for i in range(40)
        ]
        bars = pair_candles.bars_from_ohlc(rows, 30)
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[-1]["open"], 1)
        self.assertEqual(bars[-1]["high"], 3)
        self.assertEqual(bars[-1]["low"], 0.5)
        self.assertEqual(bars[-1]["close"], 2)
        self.assertEqual(bars[-1]["value"], 2)
        self.assertRegex(bars[-1]["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_rejects_a_bad_pair_without_fetching(self):
        payload = pair_candles.candles_payload("../etc/passwd", 30)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["bars"], [])


if __name__ == "__main__":
    unittest.main()

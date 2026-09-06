#!/usr/bin/env python3
"""Dry-run tests: Kraken universe, ticket math, halt. No live orders. No mechanical desk."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import desk_halt  # noqa: E402
import kraken_universe as ku  # noqa: E402

FORBIDDEN = (
    "dexscreener.com",
    "fomo.family",
    "polymarket.com",
    "kalshi.com",
    "pump.fun",
    "x.ai/bot",
)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class TicketAndRisk(unittest.TestCase):
    def test_min_ticket_uses_ordermin_times_last(self):
        need = ku.min_ticket_usd(
            {"last": 0.02, "ordermin": "600", "costmin": "0.5"}
        )
        self.assertGreater(need, 10)
        self.assertAlmostEqual(need, 12.0, places=6)

    def test_volume_ratio_close_under_20_percent(self):
        fat = [0, 0, 0, 0, 0, 0, 10.0, 0]
        thin = [0, 0, 0, 0, 0, 0, 0.5, 0]
        ohlc = [fat] * 18 + [thin] * 6
        stats = ku.volume_ratio_6h_24h(ohlc)
        self.assertIsNotNone(stats)
        self.assertLess(stats["ratio"], 0.20)

    def test_volume_ratio_hold_when_volume_steady(self):
        bar = [0, 0, 0, 0, 0, 0, 5.0, 0]
        stats = ku.volume_ratio_6h_24h([bar] * 24)
        self.assertAlmostEqual(stats["ratio"], 1.0, places=6)

    def test_halt_floor_is_half_allocated_pot(self):
        cfg = {
            "bank": {"allocatedUsd": 587, "startingUsd": 89},
            "risk": {"haltDrawdown": 0.5, "haltUntilHuman": True},
        }
        self.assertAlmostEqual(desk_halt.halt_floor_usd(cfg), 293.5, places=6)
        self.assertTrue(desk_halt.should_trip(293.5, cfg))
        self.assertFalse(desk_halt.should_trip(294.0, cfg))

    def test_round_quote_usd(self):
        self.assertEqual(ku.round_quote_usd(33.591), "33.59")
        self.assertEqual(ku.round_quote_usd(5), "5.00")


class PaperDeskShape(unittest.TestCase):
    def test_config_is_paper_kraken(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        self.assertTrue(cfg["paper"])
        self.assertEqual(cfg["venues"]["scan"], "kraken")
        self.assertEqual(cfg["venues"]["fills"], "paper")

    def test_no_mechanical_cycle_script(self):
        self.assertFalse((SCRIPTS / "paper-cycle.py").exists())
        self.assertFalse((ROOT / "systemd").exists())

    def test_scripts_have_no_forbidden_venues(self):
        for f in SCRIPTS.rglob("*"):
            if not f.is_file():
                continue
            text = f.read_text(errors="ignore").lower()
            for needle in FORBIDDEN:
                self.assertNotIn(needle, text, f"{f} cites {needle}")

    def test_universe_kraken_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["TRADING_DESK_LEDGER"] = tmp
            run = subprocess.run(
                [sys.executable, str(SCRIPTS / "kraken_universe.py")],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            universe = json.loads((Path(tmp) / "universe-kraken.json").read_text())
            self.assertGreater(universe["count"], 50)
            self.assertTrue(all(not p["pair_id"].endswith(".d") for p in universe["pairs"]))
            self.assertTrue(
                all((p["quote"] or "").upper() in {"USD", "ZUSD"} for p in universe["pairs"])
            )
            self.assertTrue(all(p.get("pair_id") for p in universe["pairs"]))

    def test_halted_flag_blocks_new_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            halt = {
                "ts": "2026-09-05T00:00:00Z",
                "status": "HALTED",
                "reason": "test",
                "resume": "human",
            }
            Path(tmp, "halt.json").write_text(json.dumps(halt))
            old = os.environ.get("TRADING_DESK_LEDGER")
            os.environ["TRADING_DESK_LEDGER"] = tmp
            try:
                self.assertTrue(desk_halt.is_halted())
            finally:
                if old is None:
                    os.environ.pop("TRADING_DESK_LEDGER", None)
                else:
                    os.environ["TRADING_DESK_LEDGER"] = old
            self.assertEqual(load_jsonl(Path(tmp) / "fills.jsonl"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

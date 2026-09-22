#!/usr/bin/env python3
"""Dry-run tests: Kraken universe, ticket math, halt, context. No AddOrder."""
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
import desk_context  # noqa: E402
import desk_halt  # noqa: E402
import kraken_universe as ku  # noqa: E402

FORBIDDEN = (
    "dexscreener.com",
    "fomo.family",
    "polymarket.com",
    "kalshi.com",
    "pump.fun",
    "x.ai/bot",
    "api.worldmonitor.app",
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

    def test_round_base_uses_lot_decimals(self):
        self.assertEqual(ku.round_base(782.36919, 5), "782.36919")
        self.assertEqual(ku.round_base(0.01330804, 8), "0.01330804")


class ContextSignals(unittest.TestCase):
    def test_build_signals_flags_fear_and_vix(self):
        cnn = {"score": 22.0, "rating": "extreme fear", "endpoint": "https://production.dataviz.cnn.io/index/fearandgreed/current"}
        crypto = {"score": 73, "rating": "Greed", "endpoint": "https://api.alternative.me/fng/?limit=1"}
        vix = {"last": 31.2, "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"}
        quotes = [
            {"symbol": "SOL", "change": 12.5, "endpoint": "https://api.coinpaprika.com/v1/tickers/sol-solana"},
            {"symbol": "BTC", "change": 0.2, "endpoint": "https://api.coinpaprika.com/v1/tickers/btc-bitcoin"},
        ]
        signals = desk_context.build_signals(cnn, crypto, vix, quotes)
        types = {s["type"] for s in signals}
        self.assertIn("CNN_FEAR_GREED", types)
        self.assertIn("CRYPTO_FEAR_GREED", types)
        self.assertIn("VIX_SPIKE", types)
        self.assertIn("CRYPTO_MOVE", types)
        self.assertTrue(any(s["id"] == "paprika:SOL" for s in signals))
        self.assertFalse(any(s["id"] == "paprika:BTC" for s in signals))

    def test_dead_vix_does_not_degrade_context(self):
        degraded, unavailable = desk_context.context_health(["vix"])
        self.assertFalse(degraded)
        self.assertFalse(unavailable)

    def test_missing_required_source_still_degrades(self):
        degraded, unavailable = desk_context.context_health(["vix", "cnn"])
        self.assertTrue(degraded)
        self.assertFalse(unavailable)

    def test_required_sources_down_is_unavailable(self):
        degraded, unavailable = desk_context.context_health(
            ["cnn", "crypto_fg", "coinpaprika", "vix"]
        )
        self.assertTrue(degraded)
        self.assertTrue(unavailable)


class DeskShape(unittest.TestCase):
    def test_config_is_live_kraken(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        self.assertFalse(cfg["paper"])
        self.assertEqual(cfg["venues"]["scan"], "kraken")
        self.assertEqual(cfg["venues"]["fills"], "kraken")

    def test_no_mechanical_cycle_script(self):
        self.assertFalse((SCRIPTS / "paper-cycle.py").exists())
        self.assertTrue((ROOT / "bin" / "run-desk-cycle").exists())
        self.assertTrue((SCRIPTS / "kraken_execute.py").exists())
        self.assertTrue((SCRIPTS / "desk_context.py").exists())

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

    def test_desk_context_writes_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["TRADING_DESK_LEDGER"] = tmp
            run = subprocess.run(
                [sys.executable, str(SCRIPTS / "desk_context.py")],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            payload = json.loads((Path(tmp) / "context.json").read_text())
            self.assertFalse(payload["unavailable"])
            self.assertIn("signals", payload)
            self.assertGreaterEqual(len(payload["signals"]), 1)

    def test_hold_rows_stay_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp)
            arb = {
                "pair_id": "ARBUSD",
                "wsname": "ARB/USD",
                "venue": "kraken",
                "filled": 35.25,
                "volume_base": 198.3,
            }
            ake = {
                "pair_id": "AKEUSD",
                "wsname": "AKE/USD",
                "venue": "kraken",
                "filled": 14.62,
                "volume_base": 782.3,
            }
            hold = {
                "pair_id": "ARBUSD",
                "wsname": "ARB/USD",
                "venue": "kraken",
                "action": "HOLD",
                "status": "open",
            }
            closed = {
                "pair_id": "AKEUSD",
                "wsname": "AKE/USD",
                "venue": "kraken",
                "action": "CLOSE",
                "status": "closed",
            }
            (ledger / "fills.jsonl").write_text(
                json.dumps(arb) + "\n" + json.dumps(ake) + "\n",
                encoding="utf-8",
            )
            (ledger / "closes.jsonl").write_text(
                json.dumps(hold) + "\n" + json.dumps(closed) + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["TRADING_DESK_LEDGER"] = tmp
            run = subprocess.run(
                [sys.executable, str(SCRIPTS / "kraken_execute.py"), "opens"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            ids = {row["pair_id"] for row in json.loads(run.stdout)["opens"]}
            self.assertEqual(ids, {"ARBUSD"})

    def test_execute_opens_does_not_order(self):
        run = subprocess.run(
            [sys.executable, str(SCRIPTS / "kraken_execute.py"), "opens"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["venue"], "kraken")
        self.assertFalse(payload["paper"])
        self.assertIn("opens", payload)

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

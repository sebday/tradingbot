#!/usr/bin/python3 -I
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
import desk_book  # noqa: E402
import desk_context  # noqa: E402
import desk_cycle  # noqa: E402
import desk_exit_check  # noqa: E402
import desk_halt  # noqa: E402
import desk_paths  # noqa: E402
import desk_rules  # noqa: E402
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
    def test_default_ledger_is_repo_not_state(self):
        old = os.environ.pop("TRADING_DESK_LEDGER", None)
        try:
            self.assertEqual(desk_paths.ledger_dir(), ROOT / "ledger")
        finally:
            if old is not None:
                os.environ["TRADING_DESK_LEDGER"] = old

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
        self.assertNotIn("edgeMin", cfg)
        self.assertEqual(cfg["scan"]["dayMoveMin"], 0)
        self.assertEqual(cfg["scan"]["dayMoveMax"], 0.15)
        self.assertEqual(cfg["risk"]["exitPollSeconds"], 300)
        self.assertEqual(cfg["risk"]["stopLossPct"], 0.08)
        self.assertIn("enabled", cfg["entry"])
        self.assertNotIn("viqc", cfg["live"].get("buyOflags", ""))

    def test_entry_timer_is_retired(self):
        self.assertTrue((ROOT / "bin" / "desk").exists())
        self.assertTrue((ROOT / "systemd" / "desk-wake.timer").exists())
        self.assertTrue((ROOT / "systemd" / "evo-trading-exit.timer").exists())
        self.assertFalse((ROOT / "bin" / "run-desk-cycle").exists())
        self.assertFalse((ROOT / "systemd" / "evo-trading-desk.timer").exists())
        self.assertFalse((ROOT / "systemd" / "omarchy-trading-desk.timer").exists())
        for seat in ("scan", "vet", "size", "fills", "risk", "book", "chief"):
            self.assertFalse((ROOT / ".cursor" / "agents" / f"{seat}.md").exists(), seat)

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


class DayBandAndExit(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config.json").read_text())

    def test_day_band_allows_an_up_day_inside_the_cap(self):
        self.assertIsNone(desk_rules.day_move_failure(0.04, self.cfg))
        self.assertIsNone(desk_rules.day_move_failure(0.15, self.cfg))

    def test_day_band_rejects_flat_red_and_spent(self):
        self.assertEqual(desk_rules.day_move_failure(0.0, self.cfg), "DAY_FLAT")
        self.assertEqual(desk_rules.day_move_failure(-0.03, self.cfg), "DAY_FLAT")
        self.assertEqual(desk_rules.day_move_failure(None, self.cfg), "DAY_FLAT")
        self.assertEqual(desk_rules.day_move_failure(0.16, self.cfg), "MOVE_SPENT")

    def test_armed_winner_ignores_dead_volume(self):
        # Peak +40% with a 5% giveback floors at 133, so 135 is still a hold.
        self.assertEqual(desk_rules.decide(100, 140, True, 135, 0.05, True, self.cfg), "HOLD")

    def test_armed_winner_closes_on_the_floor(self):
        self.assertEqual(desk_rules.decide(100, 140, True, 132, 0.05, True, self.cfg), "TRAIL_PEAK")

    def test_missing_last_is_stale_not_a_sell(self):
        self.assertEqual(desk_rules.decide(100, 140, True, None, None, False, self.cfg), "STALE")

    def test_unarmed_position_closes_on_dead_volume(self):
        self.assertEqual(desk_rules.decide(100, 105, True, 104, 0.1, True, self.cfg), "VOLUME_6H")

    def test_unarmed_position_holds_when_volume_is_alive(self):
        self.assertEqual(desk_rules.decide(100, 105, True, 104, 0.8, True, self.cfg), "HOLD")

    def test_unknown_volume_holds_instead_of_selling(self):
        self.assertEqual(desk_rules.decide(100, 105, True, 104, None, False, self.cfg), "STALE")

    def test_peak_requires_the_fill_hour(self):
        fill = 1_700_000_000
        hour = fill - (fill % 3600)
        late = [[hour + 3600, 0, 9, 0, 0, 0, 1, 0]]
        self.assertEqual(desk_rules.peak_since_fill(late, fill), (None, False))
        covered = [
            [hour, 0, 5, 0, 0, 0, 1, 0],
            [hour + 3600, 0, 9, 0, 0, 0, 1, 0],
        ]
        self.assertEqual(desk_rules.peak_since_fill(covered, fill), (9.0, True))


class StopLoss(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config.json").read_text())

    def test_stop_fires_on_the_ticker_alone(self):
        self.assertEqual(desk_rules.decide(100, None, False, 91.9, None, False, self.cfg), "STOP_LOSS")
        self.assertEqual(desk_rules.decide(100, None, False, 92.0, None, False, self.cfg), "STOP_LOSS")

    def test_stop_ignores_healthy_volume(self):
        self.assertEqual(desk_rules.decide(100, 101, True, 90, 1.5, True, self.cfg), "STOP_LOSS")

    def test_just_above_the_stop_holds(self):
        self.assertEqual(desk_rules.decide(100, 101, True, 92.5, 1.0, True, self.cfg), "HOLD")

    def test_stale_alert_fires_once_after_the_threshold(self):
        state: dict = {}
        cfg = {"risk": {"staleAlertMinutes": 30}}
        self.assertFalse(desk_exit_check.track_stale(state, "X", True, 0, cfg))
        self.assertFalse(desk_exit_check.track_stale(state, "X", True, 29 * 60, cfg))
        self.assertTrue(desk_exit_check.track_stale(state, "X", True, 30 * 60, cfg))
        self.assertFalse(desk_exit_check.track_stale(state, "X", True, 60 * 60, cfg))
        self.assertFalse(desk_exit_check.track_stale(state, "X", False, 61 * 60, cfg))
        self.assertNotIn("X", state)


def write_ledger(tmp: Path, fills: list[dict], closes: list[dict]) -> None:
    (tmp / "fills.jsonl").write_text("".join(json.dumps(r) + "\n" for r in fills), encoding="utf-8")
    (tmp / "closes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in closes), encoding="utf-8")


def fill(pair: str, ts: str, usd: float, qty: float) -> dict:
    return {"ts": ts, "pair_id": pair, "wsname": pair, "venue": "kraken", "filled": usd, "volume_base": qty}


def close(pair: str, ts: str, usd: float, qty: float, **extra) -> dict:
    row = {"ts": ts, "pair_id": pair, "venue": "kraken", "action": "CLOSE", "status": "closed",
           "filled": usd, "volume_base": qty}
    row.update(extra)
    return row


class BookEpisodes(unittest.TestCase):
    def test_reentry_after_a_full_close_is_a_new_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_ledger(
                Path(tmp),
                [
                    fill("SOLUSD", "2026-10-07T00:00:00Z", 50.0, 0.4),
                    fill("SOLUSD", "2026-10-08T00:00:00Z", 10.0, 0.1),
                    fill("SOLUSD", "2026-10-17T06:04:00Z", 20.0, 0.2),
                ],
                [close("SOLUSD", "2026-10-17T06:01:00Z", 45.0, 0.4999)],
            )
            book = desk_book.load_book("kraken", Path(tmp))
            pos = book.position("SOLUSD")
            self.assertIsNotNone(pos)
            self.assertEqual(pos.opened_ts, "2026-10-17T06:04:00Z")
            self.assertEqual(pos.n_buys, 1)
            self.assertAlmostEqual(pos.entry, 100.0)
            self.assertEqual(book.last_close_ts["SOLUSD"], "2026-10-17T06:01:00Z")
            self.assertAlmostEqual(book.closed[0].pnl_usd, -15.0)

    def test_close_dust_does_not_keep_an_episode_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_ledger(
                Path(tmp),
                [fill("PUMPUSD", "2026-10-07T00:00:00Z", 30.0, 100.0)],
                [close("PUMPUSD", "2026-10-17T00:00:00Z", 20.0, 99.99)],
            )
            self.assertEqual(desk_book.load_book("kraken", Path(tmp)).open_positions(), [])

    def test_hold_skip_and_failed_sells_keep_the_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_ledger(
                Path(tmp),
                [fill("ARBUSD", "2026-10-01T00:00:00Z", 30.0, 100.0)],
                [
                    {"ts": "2026-10-02T00:00:00Z", "pair_id": "ARBUSD", "venue": "kraken", "action": "HOLD", "status": "open"},
                    {"ts": "2026-10-03T00:00:00Z", "pair_id": "ARBUSD", "venue": "kraken", "action": "SKIP_DUST"},
                    close("ARBUSD", "2026-10-04T00:00:00Z", 0.0, 0.0, status="canceled"),
                    {"ts": "2026-10-05T00:00:00Z", "pair_id": "ARBUSD", "venue": "kraken", "action": "SELL_FAILED"},
                ],
            )
            self.assertIsNotNone(desk_book.load_book("kraken", Path(tmp)).position("ARBUSD"))

    def test_execute_opens_lists_a_reentered_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_ledger(
                Path(tmp),
                [fill("XXBTZUSD", "2026-10-10T00:00:00Z", 60.0, 0.0007),
                 fill("XXBTZUSD", "2026-10-18T03:00:00Z", 90.0, 0.0011)],
                [close("XXBTZUSD", "2026-10-17T06:00:00Z", 59.0, 0.0007)],
            )
            env = os.environ.copy()
            env["TRADING_DESK_LEDGER"] = tmp
            run = subprocess.run(
                [sys.executable, str(SCRIPTS / "kraken_execute.py"), "opens"],
                cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            opens = json.loads(run.stdout)["opens"]
            self.assertEqual([o["pair_id"] for o in opens], ["XXBTZUSD"])
            self.assertAlmostEqual(opens[0]["filled_usd"], 90.0)

    def test_reconcile_flags_untracked_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_ledger(Path(tmp), [fill("NEARUSD", "2026-10-07T00:00:00Z", 50.0, 10.0),
                                     fill("SOLUSD", "2026-10-07T00:00:00Z", 20.0, 0.2)], [])
            book = desk_book.load_book("kraken", Path(tmp))
            universe = {"pairs": [
                {"pair_id": "NEARUSD", "base": "NEAR", "wsname": "NEAR/USD"},
                {"pair_id": "SOLUSD", "base": "SOL", "wsname": "SOL/USD"},
                {"pair_id": "BABYUSD", "base": "BABY", "wsname": "BABY/USD"},
                {"pair_id": "XETHZUSD", "base": "XETH", "wsname": "ETH/USD"},
            ]}
            leftover = [{"asset": "NEAR", "amount": 10.0}, {"asset": "BABY", "amount": 3.0},
                        {"asset": "XETH", "amount": 0.05}]
            rec = desk_book.reconcile(book, leftover, universe, {"BABYUSD": 0.01, "XETHZUSD": 2400.0})
            self.assertEqual([u["pair_id"] for u in rec["untracked"]], ["XETHZUSD"])
            self.assertEqual([m["pair_id"] for m in rec["missing"]], ["SOLUSD"])


class EntryGuards(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config.json").read_text())
        self.now = desk_rules.parse_ts("2026-10-18T12:00:00Z")

    def book(self, fills, closes):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_ledger(Path(tmp.name), fills, closes)
        return desk_book.load_book("kraken", Path(tmp.name))

    def test_already_held_pair_is_blocked(self):
        b = self.book([fill("PUMPUSD", "2026-10-07T00:00:00Z", 30, 100)], [])
        self.assertEqual(desk_rules.entry_block("PUMPUSD", b, self.cfg, self.now), "ALREADY_HELD")
        self.assertIsNone(desk_rules.entry_block("SOLUSD", b, self.cfg, self.now))

    def test_reentry_cooldown(self):
        b = self.book([fill("SOLUSD", "2026-10-10T00:00:00Z", 30, 1)],
                      [close("SOLUSD", "2026-10-18T00:00:00Z", 28, 1)])
        self.assertEqual(desk_rules.entry_block("SOLUSD", b, self.cfg, self.now), "REENTRY_COOLDOWN")
        later = desk_rules.parse_ts("2026-10-19T00:30:00Z")
        self.assertIsNone(desk_rules.entry_block("SOLUSD", b, self.cfg, later))

    def test_max_new_per_day(self):
        fills = [fill(f"P{i}USD", f"2026-10-18T0{i}:00:00Z", 10, 1) for i in range(3)]
        b = self.book(fills, [])
        self.assertEqual(desk_rules.entry_block("NEWUSD", b, self.cfg, self.now), "MAX_NEW_PER_DAY")

    def test_max_open_books(self):
        fills = [fill(f"P{i}USD", f"2026-10-0{i + 1}T00:00:00Z", 10, 1) for i in range(7)]
        b = self.book(fills, [])
        self.assertEqual(desk_rules.entry_block("NEWUSD", b, self.cfg, self.now), "MAX_OPEN_BOOKS")

    def test_stablecoins_and_fiat_are_excluded(self):
        for ws in ("USDT/USD", "GBP/USD", "EURC/USD", "USDC/USD"):
            self.assertTrue(desk_rules.excluded_base({"wsname": ws}, self.cfg), ws)
        for ws in ("XBT/USD", "SOL/USD", "STBL/USD"):
            self.assertFalse(desk_rules.excluded_base({"wsname": ws}, self.cfg), ws)

    def test_flat_range_is_rejected(self):
        m = {"volume_24h_quote": 1e7, "spread": 0.0001, "pct_change": 0.002, "high_24h": 1.001, "low_24h": 0.999}
        self.assertEqual(desk_rules.scan_failure({"wsname": "ABC/USD"}, m, self.cfg), "RANGE_FLAT")
        m.update(high_24h=1.05, low_24h=0.97)
        self.assertIsNone(desk_rules.scan_failure({"wsname": "ABC/USD"}, m, self.cfg))

    def test_ticket_is_a_share_of_equity(self):
        dollars, why = desk_rules.ticket_usd(500, 400, 100, 5, self.cfg)
        self.assertIsNone(why)
        self.assertAlmostEqual(dollars, 30.0)

    def test_ticket_halves_when_degraded(self):
        dollars, _ = desk_rules.ticket_usd(500, 400, 100, 5, self.cfg, partial=True)
        self.assertAlmostEqual(dollars, 15.0)

    def test_exposure_cap_blocks_new_tickets(self):
        cfg = {**self.cfg, "entry": {**self.cfg["entry"], "maxExposurePct": 0.5}}
        self.assertEqual(desk_rules.ticket_usd(486, 164, 322, 5, cfg), (0.0, "EXPOSURE_CAP"))
        self.assertEqual(desk_rules.ticket_usd(500, 300, 245, 5, cfg), (0.0, "EXPOSURE_CAP"))
        self.assertEqual(desk_rules.ticket_usd(500, 300, 235, 5, cfg), (15.0, None))

    def test_ticket_floor_skips_dust_buys(self):
        self.assertEqual(desk_rules.ticket_usd(100, 100, 0, 5, self.cfg), (0.0, "TICKET_FLOOR"))
        self.assertEqual(desk_rules.ticket_usd(1000, 8, 0, 5, self.cfg), (0.0, "CASH"))


class VetAndPlan(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config.json").read_text())
        self.shortlist = {
            "cycle_id": "C1",
            "degraded": False,
            "candidates": [
                {"pair_id": "AUSD", "wsname": "A/USD", "rank": 1, "min_ticket_usd": 5},
                {"pair_id": "BUSD", "wsname": "B/USD", "rank": 2, "min_ticket_usd": 5},
            ],
        }
        self.empty = desk_book.Book(venue="kraken", positions={}, closed=[], last_close_ts={})
        self.now = desk_rules.parse_ts("2026-10-18T12:00:00Z")

    def test_missing_verdicts_are_vetoes(self):
        out = desk_cycle.validate_vetoes(
            self.shortlist,
            {"cycle_id": "C1", "vetoes": [{"pair_id": "AUSD", "veto": False}, {"pair_id": "ZZZ", "veto": False}]},
        )
        self.assertEqual([(v["pair_id"], v["veto"]) for v in out["vetoes"]], [("AUSD", False), ("BUSD", True)])

    def test_verdicts_for_another_cycle_are_refused(self):
        with self.assertRaises(ValueError):
            desk_cycle.validate_vetoes(self.shortlist, {"cycle_id": "C0", "vetoes": []})

    def test_no_vetoes_file_means_no_buys(self):
        plan = desk_cycle.plan_entries(self.cfg, self.shortlist, None, self.empty, 500, 500, False, self.now)
        self.assertTrue(all(d["dollars"] == 0 and d["reason"] == "VET_MISSING" for d in plan))

    def test_one_buy_per_cycle_and_vetoes_respected(self):
        vetoes = {"cycle_id": "C1", "vetoes": [{"pair_id": "AUSD", "veto": False}, {"pair_id": "BUSD", "veto": False}]}
        plan = desk_cycle.plan_entries(self.cfg, self.shortlist, vetoes, self.empty, 500, 500, False, self.now)
        self.assertEqual([d["reason"] for d in plan], ["PASS", "MAX_FILLS_PER_CYCLE"])
        self.assertAlmostEqual(plan[0]["dollars"], 30.0)
        vetoes["vetoes"][0]["veto"] = True
        plan = desk_cycle.plan_entries(self.cfg, self.shortlist, vetoes, self.empty, 500, 500, False, self.now)
        self.assertEqual([d["reason"] for d in plan], ["LLM_VETO", "PASS"])

    def test_halt_blocks_entries(self):
        vetoes = {"cycle_id": "C1", "vetoes": [{"pair_id": "AUSD", "veto": False}]}
        plan = desk_cycle.plan_entries(self.cfg, self.shortlist, vetoes, self.empty, 500, 500, True, self.now)
        self.assertEqual(plan[0]["reason"], "HALTED")


class LimitPricing(unittest.TestCase):
    def test_limit_is_ask_plus_slippage_cap(self):
        cfg = {"entry": {"maxSlippageBps": 40}}
        self.assertAlmostEqual(desk_rules.limit_buy_price(100.0, cfg), 100.4)

    def test_price_and_volume_round_down(self):
        self.assertEqual(ku.round_price(0.0063369, 6), "0.006336")
        self.assertEqual(ku.round_base(30 / 0.006336, 0), "4734")
        self.assertEqual(ku.round_base(0.123456789, 8), "0.12345678")


if __name__ == "__main__":
    unittest.main(verbosity=2)

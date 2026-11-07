"""Preflight sensors -> [{name, ok, fatal, detail}]. Any fatal failure => observe-only cycle."""
from __future__ import annotations

import json
import time

from . import paths, policy
from .book import load_book
from .journal import SCHEMA_VERSION
from .util import parse_ts, read_json, utc_now


def _check(name: str, ok: bool, detail, fatal: bool = True) -> dict:
    return {"name": name, "ok": bool(ok), "fatal": fatal, "detail": detail}


def legacy_halted() -> dict | None:
    row = read_json(paths.ledger_dir() / "halt.json")
    return row if row and row.get("status") == "HALTED" else None


def is_halted(journal) -> dict | None:
    return journal.halt_state() or legacy_halted()


def trip_halt(journal, cfg: dict, equity: float, source: str) -> dict:
    row = {"ts": utc_now(), "status": "HALTED", "source": source, "equity_usd": round(float(equity), 2),
           "floor_usd": round(policy.halt_floor_usd(cfg), 2), "allocated_usd": round(policy.allocated_usd(cfg), 2),
           "reason": f"equity {equity:.2f} at or below floor {policy.halt_floor_usd(cfg):.2f}", "resume": "human"}
    journal.set_halt(row)
    journal.add_incident("HALT_TRIPPED", json.dumps(row), "halt_floor", "test_rule_halted")
    return row


def seat_isolation() -> tuple[bool, dict]:
    seat = paths.seat_dir()
    cli = read_json(seat / ".cursor" / "cli.json", {}) or {}
    mcp = read_json(seat / ".cursor" / "mcp.json", {}) or {}
    deny = set(((cli.get("permissions") or {}).get("deny")) or [])
    allow = set(((cli.get("permissions") or {}).get("allow")) or [])
    servers = sorted((mcp.get("mcpServers") or {}).keys())
    need = {"Shell(*)", "Write(**)"}
    ok = need <= deny and servers == ["desk"] and not any(a.startswith("Shell") or a.startswith("Write") for a in allow)
    ok = ok and (seat / "AGENTS.md").exists()
    return ok, {"deny": sorted(deny), "allow": sorted(allow), "mcp_servers": servers,
                "agents_md": (seat / "AGENTS.md").exists()}


def preflight(cfg: dict, journal, market, broker, venue: str, now: float | None = None,
              check_seat: bool = True) -> tuple[list[dict], dict]:
    """Returns (checks, facts). facts carries what was measured (balance, universe, tickers, context)."""
    now = time.time() if now is None else now
    checks: list[dict] = []
    facts: dict = {}
    h = cfg.get("harness") or {}
    sens = h.get("sensors") or {}

    checks.append(_check("config", True, {"mode": h.get("mode")}))
    sv = journal.schema_version()
    checks.append(_check("schema", sv == SCHEMA_VERSION, {"schema_version": sv, "expected": SCHEMA_VERSION}))

    halt = is_halted(journal)
    checks.append(_check("halt_clear", halt is None, halt or "clear"))

    try:
        uni = market.universe(max_age_hours=float(sens.get("maxUniverseAgeHours", 24)))
        pairs = uni.get("pairs") or []
        facts["universe"] = uni
        checks.append(_check("universe", len(pairs) > 0, {"count": len(pairs), "fetchedAt": uni.get("fetchedAt")}))
    except Exception as exc:  # noqa: BLE001
        pairs = []
        checks.append(_check("universe", False, f"{type(exc).__name__}: {exc}"[:300]))

    try:
        ticks = market.tickers([p["pair_id"] for p in pairs]) if pairs else {}
        facts["tickers"] = ticks
        checks.append(_check("ticker_fresh", len(ticks) > 0, {"count": len(ticks)}))
    except Exception as exc:  # noqa: BLE001
        facts["tickers"] = {}
        checks.append(_check("ticker_fresh", False, f"{type(exc).__name__}: {exc}"[:300]))

    from .market import context as ctx
    c = ctx.load()
    age_min = None
    if c and parse_ts(c.get("fetchedAt")):
        age_min = (now - parse_ts(c["fetchedAt"])) / 60.0
    fresh = c is not None and age_min is not None and age_min <= float(sens.get("maxContextAgeMinutes", 120))
    degraded = (not fresh) or bool((c or {}).get("degraded") or (c or {}).get("unavailable"))
    facts["context"] = c
    facts["context_degraded"] = degraded
    # Not fatal: a degraded context halves the ticket, as the legacy desk does.
    checks.append(_check("context", fresh and not degraded,
                         {"age_minutes": None if age_min is None else round(age_min, 1),
                          "errors": (c or {}).get("errors")}, fatal=False))

    try:
        bal = broker.balance()
        facts["balance"] = bal
        eq = float(bal.get("equity_usd") or 0)
        checks.append(_check("balance", eq > 0, {"equity_usd": eq, "usd_spot": bal.get("usd_spot"),
                                                 "source": bal.get("source")}))
        if venue == "kraken" and policy.should_trip(eq, cfg) and halt is None:
            row = trip_halt(journal, cfg, eq, "sensors")
            checks.append(_check("halt_floor", False, row))
        else:
            checks.append(_check("halt_floor", not policy.should_trip(eq, cfg) or venue != "kraken",
                                 {"equity_usd": eq, "floor_usd": round(policy.halt_floor_usd(cfg), 2)}))
    except Exception as exc:  # noqa: BLE001
        facts["balance"] = None
        checks.append(_check("balance", False, f"{type(exc).__name__}: {exc}"[:300]))

    if venue == "kraken":
        rec = journal.meta_json("reconcile:kraken")
        ok = bool(rec) and rec.get("clean") and (now - (parse_ts(rec.get("ts")) or 0)) < 3 * 3600
        checks.append(_check("reconcile", ok, rec or "never reconciled"))
        book = load_book(journal, "kraken")
        missing_stops = [p.pair_id for p in book.open_positions() if p.stop_price is None]
        checks.append(_check("stops_present", not missing_stops, {"missing": missing_stops}))
    else:
        book = load_book(journal, "paper")
        missing_stops = [p.pair_id for p in book.open_positions() if p.stop_price is None]
        checks.append(_check("stops_present", not missing_stops, {"missing": missing_stops}))
        if book.positions:
            last = journal.last_run("exits")
            age = now - (parse_ts((last or {}).get("started_at")) or 0)
            checks.append(_check("exits_running", age < 20 * 60, {"last_exits": (last or {}).get("started_at")}))

    if check_seat:
        ok, detail = seat_isolation()
        checks.append(_check("seat_isolation", ok, detail))
    return checks, facts


def observe_only(checks: list[dict]) -> list[str]:
    return [c["name"] for c in checks if c["fatal"] and not c["ok"]]

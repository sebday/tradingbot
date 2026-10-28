"""What Grok sees at the start of an hour: book, board, context, and why the last trades were placed."""
from __future__ import annotations

from .config import live, utc_now
from .rules_lib import as_dict


def build(cfg: dict, log, services) -> dict:
    out: dict = {
        "ts": utc_now(),
        "live": live(cfg),
        "halted": bool(services.entries_blocked()),
        "equity_usd": None,
        "usd_spot": None,
        "positions": [],
        "exit_rules": [],
        "entries": [],
        "recent_trades": [],
        "context": {},
        "board": [],
        "errors": [],
    }
    try:
        bal = services.balance()
        out["equity_usd"] = bal.get("equity_usd")
        out["usd_spot"] = bal.get("usd_spot")
    except Exception as exc:
        out["errors"].append(f"balance: {exc}")

    positions = []
    try:
        positions = list(services.positions())
    except Exception as exc:
        out["errors"].append(f"book: {exc}")
    ids = [p.pair_id for p in positions]
    ticks = {}
    try:
        ticks = services.tickers(ids) if ids else {}
    except Exception as exc:
        out["errors"].append(f"tickers: {exc}")
    for pos in positions:
        tick = ticks.get(pos.pair_id) or {}
        last = tick.get("last")
        pnl = None
        if last and pos.qty and pos.entry:
            pnl = round((float(last) - pos.entry) * pos.qty, 2)
        closes = []
        try:
            ohlc = services.ohlc(pos.pair_id) or []
            closes = [float(c[4]) for c in ohlc[-8:]]
        except Exception:
            closes = []
        out["positions"].append({
            "pair_id": pos.pair_id,
            "wsname": pos.wsname,
            "qty": pos.qty,
            "entry": pos.entry,
            "opened_ts": pos.opened_ts,
            "last": last,
            "pnl_usd": pnl,
            "closes_1h": closes,
        })

    for rule in log.active_rules():
        out["exit_rules"].append({
            "pair_id": rule["pair_id"],
            "name": rule["name"],
            "params": as_dict(rule["params_json"]),
            "reason": rule["reason"],
            "opened_ts": rule.get("opened_ts"),
        })
    for entry in log.waiting_entries():
        out["entries"].append({
            "id": entry["id"],
            "pair_id": entry["pair_id"],
            "kind": entry["kind"],
            "size_usd": entry["size_usd"],
            "params": as_dict(entry["params_json"]),
            "exit_rule": as_dict(entry["exit_rule_json"]),
            "reason": entry["reason"],
            "expires_at": entry.get("expires_at"),
            "kraken_txid": entry.get("kraken_txid"),
            "last_error": entry.get("last_error"),
        })
    for trade in log.recent_trades(8):
        out["recent_trades"].append({
            "ts": trade["ts"], "side": trade["side"], "pair_id": trade["pair_id"],
            "size_usd": trade["size_usd"], "qty": trade["qty"], "price": trade["price"],
            "status": trade["status"], "reason": trade["reason"], "rule_name": trade["rule_name"],
        })
    out["context"] = _context()
    out["board"] = _board(cfg, services, out["errors"])
    return out


def _context() -> dict:
    import sys

    from .config import ROOT

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import desk_context

        payload = desk_context.fetch_all()
        desk_context.write_context(payload)
    except Exception as exc:
        return {"error": str(exc)}
    fg = payload.get("cryptoFearGreed") or {}
    vix = payload.get("vix") or {}
    return {
        "degraded": bool(payload.get("degraded") or payload.get("unavailable")),
        "crypto_fear_greed": fg.get("value") if isinstance(fg, dict) else fg,
        "vix": vix.get("price") if isinstance(vix, dict) else vix,
        "signals": [
            {"name": s.get("name") or s.get("asset") or s.get("id"), "title": s.get("title") or s.get("headline")}
            for s in (payload.get("signals") or [])[:8]
        ],
        "errors": payload.get("errors") or [],
    }


def _board(cfg: dict, services, errors: list) -> list:
    import sys

    from .config import ROOT

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import desk_rules
        import kraken_universe as ku

        universe = ku.load_universe()
        pairs = universe.get("pairs") or []
        ticks = ku.fetch_tickers([p["pair_id"] for p in pairs])
    except Exception as exc:
        errors.append(f"board: {exc}")
        return []
    rows = []
    for pair in pairs:
        if desk_rules.excluded_base(pair, cfg):
            continue
        tick = ticks.get(pair["pair_id"])
        if not tick:
            continue
        rows.append({
            "pair_id": pair["pair_id"],
            "wsname": pair.get("wsname"),
            "last": tick.get("last"),
            "pct_change": None if tick.get("pct_change") is None else round(float(tick["pct_change"]), 4),
            "spread": None if tick.get("spread") is None else round(float(tick["spread"]), 5),
            "volume_24h_quote": round(float(tick.get("volume_24h_quote") or 0)),
        })
    rows.sort(key=lambda r: -r["volume_24h_quote"])
    return rows[:12]

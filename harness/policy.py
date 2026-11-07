"""Pure policy. No I/O, no clock, no network. Ported from scripts/desk_rules.py.

Every hard rule has an id in RULES. Each id must appear in seat/AGENTS.md and
have a test named test_rule_<id lowercased> (doctor checks both).

Stop semantics:
  * Legacy positions (imported) keep the stop they had: entry * (1 - risk.stopLossPct)
    computed once at import and frozen in the journal. legacy_stop_price() is only
    called by import-legacy, never by exits or the gate.
  * New entries carry a model-proposed stop_price that must sit between
    harness.entry.minStopPct and maxStopPct under the ask.
  * A stop can be tightened (raised), never loosened.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import iso, parse_ts

STOP_LOSS = "STOP_LOSS"
TRAIL_PEAK = "TRAIL_PEAK"
VOLUME_6H = "VOLUME_6H"
HOLD = "HOLD"
STALE = "STALE"
CLOSE_RULES = (STOP_LOSS, TRAIL_PEAK, VOLUME_6H)

DEFAULT_EXCLUDE = (
    "USDT", "USDC", "DAI", "PYUSD", "USDG", "RLUSD", "USDS", "USDE", "FDUSD", "TUSD", "USDQ", "USDR",
    "EURC", "EURQ", "EURR", "EUR", "GBP", "AUD", "CAD", "CHF", "JPY",
)

ACTIONS = ("open", "close", "tighten_stop", "hold")

# Hard rules. id -> one-line statement (mirrored in seat/AGENTS.md).
RULES = {
    "HALTED": "No opens while the desk is halted; only a human resumes.",
    "ACTION_NOT_ALLOWED": "Only actions listed in the mandate are allowed.",
    "NOT_IN_UNIVERSE": "Opens only on pairs in the mandate universe.",
    "ALREADY_HELD": "Never add to a pair already held.",
    "REENTRY_COOLDOWN": "Wait entry.reentryCooldownHours after a close before re-entering a pair.",
    "MAX_OPEN_BOOKS": "At most maxOpenBooks open positions.",
    "MAX_NEW_PER_DAY": "At most entry.maxNewPerDay new positions per rolling 24h.",
    "MAX_FILLS_PER_CYCLE": "At most live.maxFillsPerCycle opens per cycle.",
    "STOP_REQUIRED": "Every open needs a stop_price.",
    "STOP_TOO_TIGHT": "A new stop must be at least harness.entry.minStopPct under the ask.",
    "STOP_TOO_WIDE": "A new stop must be at most harness.entry.maxStopPct under the ask.",
    "STOP_LOOSEN": "A stop can only be tightened (raised), never loosened.",
    "TICKET_CAP": "size_usd must not exceed the ticket cap (entry.ticketPctEquity of equity).",
    "EXPOSURE_CAP": "Total exposure must stay within entry.maxExposurePct of equity.",
    "CASH": "Size must be covered by cash after fees.",
    "TICKET_FLOOR": "Size must clear entry.minTicketUsd and the Kraken order minimum plus fees.",
    "BUDGET": "Opens in a cycle must fit the mandate dollar budget.",
    "NOT_HELD": "close/tighten_stop only on held pairs.",
    "NO_PRICE": "No order without a fresh ask/bid.",
    "DUPLICATE_PAIR": "One decision per pair per submission.",
}


# ---------------------------------------------------------------- exits (ported)

def peak_since_fill(ohlc: list, fill_epoch: float | None) -> tuple[float | None, bool]:
    """Max hourly high from the fill hour. Unknown if the series starts later."""
    if not ohlc or fill_epoch is None:
        return None, False
    hour = int(fill_epoch) - (int(fill_epoch) % 3600)
    oldest = int(float(ohlc[0][0]))
    if oldest > hour:
        return None, False
    highs = [float(c[2]) for c in ohlc if int(float(c[0])) >= hour]
    if not highs:
        return None, False
    return max(highs), True


def trail_floor(entry: float, peak: float, cfg: dict) -> float:
    risk = cfg.get("risk") or {}
    giveback = float(risk.get("trailGiveback", 0.2))
    floor_pct = float(risk.get("trailFloorPct", 0.1))
    return max(peak * (1.0 - giveback), entry * (1.0 + floor_pct))


def trail_armed(entry: float, peak: float | None, peak_known: bool, cfg: dict) -> bool:
    if not peak_known or peak is None or entry <= 0:
        return False
    arm = float((cfg.get("risk") or {}).get("trailArmPct", 0.3))
    return (peak - entry) / entry >= arm


def legacy_stop_price(entry: float, cfg: dict) -> float:
    """The stop the legacy desk applies: risk.stopLossPct under entry. Import-only."""
    stop = float((cfg.get("risk") or {}).get("stopLossPct", 0.08))
    return entry * (1.0 - stop)


def decide(
    entry: float,
    stop: float | None,
    peak: float | None,
    peak_known: bool,
    last: float | None,
    ratio: float | None,
    volume_known: bool,
    cfg: dict,
) -> str:
    """STOP_LOSS, TRAIL_PEAK, VOLUME_6H, HOLD, or STALE. Same order as desk_rules.decide,
    but the stop is the position's stored stop_price rather than recomputed from entry.
    STALE means the position could not be measured: held, never sold. A missing stop
    is STALE too (never guess a stop)."""
    if last is None or entry <= 0 or stop is None or stop <= 0:
        return STALE
    if last <= stop:
        return STOP_LOSS
    if trail_armed(entry, peak, peak_known, cfg):
        return TRAIL_PEAK if last <= trail_floor(entry, peak, cfg) else HOLD
    if not volume_known or ratio is None:
        return STALE
    vol_close = float((cfg.get("risk") or {}).get("volumeRatioClose", 0.2))
    return VOLUME_6H if ratio < vol_close else HOLD


# ---------------------------------------------------------------- scan (ported)

def day_move_failure(pct: float | None, cfg: dict) -> str | None:
    scan = cfg.get("scan") or {}
    lo = float(scan.get("dayMoveMin", 0))
    hi = float(scan.get("dayMoveMax", 0.15))
    if pct is None or pct <= lo:
        return "DAY_FLAT"
    if pct > hi:
        return "MOVE_SPENT"
    return None


def pair_base(pair: dict) -> str:
    ws = str(pair.get("wsname") or "")
    if "/" in ws:
        return ws.split("/", 1)[0].upper()
    return str(pair.get("base") or pair.get("altname") or "").upper()


def excluded_base(pair: dict, cfg: dict) -> bool:
    bases = (cfg.get("scan") or {}).get("excludeBases")
    bases = DEFAULT_EXCLUDE if bases is None else bases
    return pair_base(pair) in {str(b).upper() for b in bases}


def range_too_flat(metrics: dict, cfg: dict) -> bool:
    floor = float((cfg.get("scan") or {}).get("minRange24h", 0.01))
    high = metrics.get("high_24h")
    low = metrics.get("low_24h")
    if not high or not low:
        return False
    return (float(high) - float(low)) / float(low) < floor


def scan_failure(pair: dict, metrics: dict, cfg: dict) -> str | None:
    scan = cfg.get("scan") or {}
    if excluded_base(pair, cfg):
        return "EXCLUDED_BASE"
    if float(metrics.get("volume_24h_quote") or 0) < float(scan.get("volume24hMin", 100000)):
        return "VOLUME"
    spread = metrics.get("spread")
    if spread is None or float(spread) > float(scan.get("maxSpread", 0.004)):
        return "SPREAD"
    band = day_move_failure(metrics.get("pct_change"), cfg)
    if band:
        return band
    if range_too_flat(metrics, cfg):
        return "RANGE_FLAT"
    return None


def min_ticket_usd(pair: dict, last: float) -> float:
    by_order = float(pair.get("ordermin") or 0) * float(last or 0)
    by_cost = float(pair["costmin"]) if pair.get("costmin") else 0.0
    return max(by_order, by_cost)


def rank_shortlist(universe_pairs: list[dict], tickers: dict, book, cfg: dict, now_epoch: float) -> dict:
    """Same ranking as scripts/desk_cycle.build_shortlist: gates, book guards,
    sort by (-volume_24h_quote, -trades_24h), keep scan.maxCandidates."""
    index = {p["pair_id"]: p for p in universe_pairs}
    passed, blocked, gates = [], [], {}
    for pid, m in tickers.items():
        pair = index.get(pid)
        if not pair:
            continue
        failure = scan_failure(pair, m, cfg)
        if failure:
            gates[failure] = gates.get(failure, 0) + 1
            continue
        row = {
            "pair_id": pid, "wsname": pair.get("wsname") or "", "altname": pair.get("altname") or "",
            "base": pair_base(pair), "last": m["last"], "ask": m["ask"], "bid": m["bid"], "spread": m["spread"],
            "pct_change": m["pct_change"], "high_24h": m.get("high_24h"), "low_24h": m.get("low_24h"),
            "volume_24h_quote": m["volume_24h_quote"], "trades_24h": m["trades_24h"],
            "ordermin": pair.get("ordermin"), "costmin": pair.get("costmin"),
            "min_ticket_usd": min_ticket_usd(pair, m["last"]), "taker_fee": pair.get("taker_fee"),
            "pair_decimals": pair.get("pair_decimals"), "lot_decimals": pair.get("lot_decimals"),
        }
        block = entry_block(pid, book, cfg, now_epoch)
        if block:
            gates[block] = gates.get(block, 0) + 1
            blocked.append({**row, "failed_check": block})
            continue
        passed.append(row)
    passed.sort(key=lambda r: (-float(r["volume_24h_quote"]), -int(r["trades_24h"])))
    limit = int((cfg.get("scan") or {}).get("maxCandidates", 10))
    shortlist = passed[:limit]
    for i, row in enumerate(shortlist, start=1):
        row["rank"] = i
    return {"candidates": shortlist, "blocked": blocked, "gates": gates}


# ---------------------------------------------------------------- entries (ported)

def entry_block(pair_id: str, book, cfg: dict, now_epoch: float) -> str | None:
    entry = cfg.get("entry") or {}
    if book.position(pair_id) is not None:
        return "ALREADY_HELD"
    cooldown_h = float(entry.get("reentryCooldownHours", 24))
    closed = parse_ts(book.last_close_ts.get(pair_id))
    if closed is not None and now_epoch - closed < cooldown_h * 3600:
        return "REENTRY_COOLDOWN"
    if len(book.positions) >= int(cfg.get("maxOpenBooks") or 7):
        return "MAX_OPEN_BOOKS"
    if book.opened_since(iso(now_epoch - 86400)) >= int(entry.get("maxNewPerDay", 3)):
        return "MAX_NEW_PER_DAY"
    return None


def ticket_usd(equity_usd: float, usd_spot: float, invested_usd: float, pair_min_usd: float, cfg: dict,
               partial: bool = False) -> tuple[float, str | None]:
    entry = cfg.get("entry") or {}
    fee = float((cfg.get("fee") or {}).get("rate") or 0.004)
    want = equity_usd * float(entry.get("ticketPctEquity", 0.06))
    if partial:
        want *= 0.5
    room = equity_usd * float(entry.get("maxExposurePct", 0.5)) - invested_usd
    if room <= 0:
        return 0.0, "EXPOSURE_CAP"
    cash = usd_spot * (1.0 - fee)
    dollars = min(want, room, cash)
    floor = max(float(entry.get("minTicketUsd", 10)), pair_min_usd * (1.0 + fee))
    if dollars < floor:
        if room < floor:
            return 0.0, "EXPOSURE_CAP"
        if cash < floor:
            return 0.0, "CASH"
        return 0.0, "TICKET_FLOOR"
    return round(dollars, 2), None


def limit_buy_price(ask: float, cfg: dict) -> float:
    bps = float((cfg.get("entry") or {}).get("maxSlippageBps", 40))
    return ask * (1.0 + bps / 10000.0)


# ---------------------------------------------------------------- halt (ported)

def allocated_usd(cfg: dict) -> float:
    bank = cfg.get("bank") or {}
    if bank.get("allocatedUsd") is not None:
        return float(bank["allocatedUsd"])
    return float(bank.get("startingUsd") or 0)


def halt_floor_usd(cfg: dict) -> float:
    drawdown = float((cfg.get("risk") or {}).get("haltDrawdown") or 0.5)
    return allocated_usd(cfg) * (1.0 - drawdown)


def should_trip(equity_usd: float, cfg: dict) -> bool:
    if not (cfg.get("risk") or {}).get("haltUntilHuman", True):
        return False
    return float(equity_usd) <= halt_floor_usd(cfg) + 1e-9


# ---------------------------------------------------------------- stops for new entries

def stop_band(ask: float, cfg: dict) -> tuple[float, float]:
    """(lowest allowed stop, highest allowed stop) for a new entry at this ask."""
    h = (cfg.get("harness") or {}).get("entry") or {}
    lo_pct = float(h.get("minStopPct", 0.03))
    hi_pct = float(h.get("maxStopPct", 0.08))
    return ask * (1.0 - hi_pct), ask * (1.0 - lo_pct)


# ---------------------------------------------------------------- gate

@dataclass
class GateState:
    """Everything the gate needs, captured as plain data so replay is exact."""
    cfg: dict
    book: object               # book.Book (positions, last_close_ts, opened_since)
    equity_usd: float
    usd_spot: float
    now_epoch: float
    halted: bool
    allowed_actions: list
    universe: dict             # pair_id -> candidate row (ask, bid, min_ticket_usd, ...)
    tickers: dict              # pair_id -> {ask, bid, last}
    stops: dict                # pair_id -> current stop for held pairs
    budget_usd: float
    max_fills: int
    partial: bool = False
    approved_opens: int = 0
    spent_usd: float = 0.0
    invested_usd: float | None = None
    opened_pairs: list = field(default_factory=list)


def _invested(state: GateState) -> float:
    if state.invested_usd is not None:
        return state.invested_usd
    return max(0.0, state.equity_usd - state.usd_spot)


def gate_one(p: dict, state: GateState) -> dict:
    """Decide one proposal. Returns {approved, reasons, action, pair_id, size_usd, limit_price, stop_price, ask}.
    Mutates state counters for approved opens so later proposals see the spend."""
    action = p.get("action")
    pid = p.get("pair_id")
    out = {"action": action, "pair_id": pid, "approved": False, "reasons": [], "size_usd": None,
           "limit_price": None, "stop_price": None, "ask": None}
    reasons = out["reasons"]
    if action not in state.allowed_actions:
        reasons.append("ACTION_NOT_ALLOWED")
        return out
    if action == "hold":
        out["approved"] = True
        return out

    cfg = state.cfg
    held = state.book.position(pid) if pid else None
    tick = state.tickers.get(pid) or {}

    if action == "open":
        if state.halted:
            reasons.append("HALTED")
        cand = state.universe.get(pid)
        if cand is None:
            reasons.append("NOT_IN_UNIVERSE")
        if pid in state.opened_pairs:
            reasons.append("ALREADY_HELD")
        block = entry_block(pid, state.book, cfg, state.now_epoch) if pid else None
        if block:
            reasons.append(block)
        # opens approved earlier in this cycle count toward the book limits
        if state.approved_opens and not block:
            if len(state.book.positions) + state.approved_opens >= int(cfg.get("maxOpenBooks") or 7):
                reasons.append("MAX_OPEN_BOOKS")
            since = iso(state.now_epoch - 86400)
            per_day = int((cfg.get("entry") or {}).get("maxNewPerDay", 3))
            if state.book.opened_since(since) + state.approved_opens >= per_day:
                reasons.append("MAX_NEW_PER_DAY")
        if state.approved_opens >= state.max_fills:
            reasons.append("MAX_FILLS_PER_CYCLE")
        ask = tick.get("ask")
        if not ask or float(ask) <= 0:
            reasons.append("NO_PRICE")
            return _dedupe(out)
        ask = float(ask)
        out["ask"] = ask
        stop = p.get("stop_price")
        if stop is None:
            reasons.append("STOP_REQUIRED")
        else:
            lo, hi = stop_band(ask, cfg)
            stop = float(stop)
            if stop > hi + 1e-12:
                reasons.append("STOP_TOO_TIGHT")
            if stop < lo - 1e-12:
                reasons.append("STOP_TOO_WIDE")
            out["stop_price"] = stop
        size = float(p.get("size_usd") or 0)
        pair_min = float((cand or {}).get("min_ticket_usd") or 0)
        spot_left = state.usd_spot - state.spent_usd
        cap, why = ticket_usd(state.equity_usd, spot_left, _invested(state) + state.spent_usd, pair_min, cfg,
                              partial=state.partial)
        if why:
            reasons.append(why)
        elif size > cap + 1e-9:
            reasons.append("TICKET_CAP")
        fee = float((cfg.get("fee") or {}).get("rate") or 0.004)
        floor = max(float((cfg.get("entry") or {}).get("minTicketUsd", 10)), pair_min * (1.0 + fee))
        if size < floor:
            reasons.append("TICKET_FLOOR")
        if size > state.budget_usd - state.spent_usd + 1e-9:
            reasons.append("BUDGET")
        out["size_usd"] = round(size, 2)
        out["limit_price"] = limit_buy_price(ask, cfg)
        if not reasons:
            out["approved"] = True
            state.approved_opens += 1
            state.spent_usd += size
            state.opened_pairs.append(pid)
        return _dedupe(out)

    if action == "close":
        if held is None:
            reasons.append("NOT_HELD")
        bid = tick.get("bid")
        if not bid:
            reasons.append("NO_PRICE")
        out["ask"] = tick.get("ask")
        out["approved"] = not reasons
        return _dedupe(out)

    if action == "tighten_stop":
        if held is None:
            reasons.append("NOT_HELD")
            return _dedupe(out)
        stop = p.get("stop_price")
        current = state.stops.get(pid)
        if stop is None:
            reasons.append("STOP_REQUIRED")
            return _dedupe(out)
        stop = float(stop)
        out["stop_price"] = stop
        if current is not None and stop <= float(current) + 1e-12:
            reasons.append("STOP_LOOSEN")
        bid = tick.get("bid")
        if not bid:
            reasons.append("NO_PRICE")
        elif stop >= float(bid):
            # a stop at or over the bid is a close in disguise; ask for close instead
            reasons.append("STOP_TOO_TIGHT")
        out["approved"] = not reasons
        return _dedupe(out)

    reasons.append("ACTION_NOT_ALLOWED")
    return _dedupe(out)


def gate(proposals: list[dict], state: GateState) -> list[dict]:
    """Pure: decide each proposal in order. At most one decision per pair (first wins)."""
    seen: set = set()
    out = []
    for p in proposals:
        if p.get("action") != "hold" and p.get("pair_id") in seen:
            d = {"action": p.get("action"), "pair_id": p.get("pair_id"), "approved": False,
                 "reasons": ["DUPLICATE_PAIR"], "size_usd": None, "limit_price": None, "stop_price": None, "ask": None}
        else:
            d = gate_one(p, state)
            if p.get("action") != "hold":
                seen.add(p.get("pair_id"))
        out.append(d)
    return out


def _dedupe(out: dict) -> dict:
    seen, uniq = set(), []
    for r in out["reasons"]:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    out["reasons"] = uniq
    if uniq:
        out["approved"] = False
    return out

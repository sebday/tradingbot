#!/usr/bin/python3 -I
"""Pure desk rules: exits, entry guards, sizing, limit pricing. No I/O."""
from __future__ import annotations

from datetime import datetime, timezone

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


def parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- exits


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


def stop_price(entry: float, cfg: dict) -> float:
    stop = float((cfg.get("risk") or {}).get("stopLossPct", 0.08))
    return entry * (1.0 - stop)


def decide(
    entry: float,
    peak: float | None,
    peak_known: bool,
    last: float | None,
    ratio: float | None,
    volume_known: bool,
    cfg: dict,
) -> str:
    """STOP_LOSS, TRAIL_PEAK, VOLUME_6H, HOLD, or STALE.

    STALE means the position could not be measured. It is held, never sold.
    """
    if last is None or entry <= 0:
        return STALE
    if last <= stop_price(entry, cfg):
        return STOP_LOSS
    if trail_armed(entry, peak, peak_known, cfg):
        return TRAIL_PEAK if last <= trail_floor(entry, peak, cfg) else HOLD
    if not volume_known or ratio is None:
        return STALE
    vol_close = float((cfg.get("risk") or {}).get("volumeRatioClose", 0.2))
    return VOLUME_6H if ratio < vol_close else HOLD


# ---------------------------------------------------------------- entries


def day_move_failure(pct: float | None, cfg: dict) -> str | None:
    """Day band. None means the move is allowed."""
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
    """A 24h high/low range under scan.minRange24h is a peg, not a long."""
    floor = float((cfg.get("scan") or {}).get("minRange24h", 0.01))
    high = metrics.get("high_24h")
    low = metrics.get("low_24h")
    if not high or not low:
        return False
    return (float(high) - float(low)) / float(low) < floor


def scan_failure(pair: dict, metrics: dict, cfg: dict) -> str | None:
    """Mechanical SCAN/VET gates, cheapest first."""
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


def entry_block(pair_id: str, book, cfg: dict, now_epoch: float) -> str | None:
    """Book-level guards. None means a new episode on pair_id is allowed."""
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


def ticket_usd(
    equity_usd: float,
    usd_spot: float,
    invested_usd: float,
    pair_min_usd: float,
    cfg: dict,
    partial: bool = False,
) -> tuple[float, str | None]:
    """Dollars for one new episode, or (0, reason)."""
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

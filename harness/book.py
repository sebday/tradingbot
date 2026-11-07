"""Positions and episodes rebuilt from the journal. Same semantics as scripts/desk_book.py:
one open episode per pair; an episode opens on the first fill while flat and ends on a
CLOSE row; a re-buy after a full close is a new episode."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .util import utc_now

FAILED_SELL = {"canceled", "cancelled", "expired"}


@dataclass
class Position:
    pair_id: str
    wsname: str
    venue: str
    qty: float = 0.0
    cost_usd: float = 0.0
    opened_ts: str | None = None
    last_fill_ts: str | None = None
    n_buys: int = 0
    source: str = "journal"
    stop_price: float | None = None
    stop_source: str | None = None
    fills: list = field(default_factory=list)

    @property
    def entry(self) -> float:
        return self.cost_usd / self.qty if self.qty > 0 else 0.0

    def to_dict(self) -> dict:
        row = asdict(self)
        row.pop("fills")
        row["entry"] = self.entry
        row["volume_base"] = self.qty
        row["filled_usd"] = self.cost_usd
        row["first_ts"] = self.opened_ts
        return row


@dataclass
class ClosedEpisode:
    pair_id: str
    wsname: str
    opened_ts: str | None
    closed_ts: str
    cost_usd: float
    proceeds_usd: float
    n_buys: int
    rule_fired: str | None
    fee_usd: float = 0.0

    @property
    def pnl_usd(self) -> float:
        return self.proceeds_usd - self.cost_usd


@dataclass
class Book:
    venue: str
    positions: dict
    closed: list
    last_close_ts: dict

    def open_positions(self) -> list:
        return sorted(self.positions.values(), key=lambda p: p.opened_ts or "")

    def position(self, pair_id: str):
        return self.positions.get(pair_id)

    def opened_since(self, since_ts: str) -> int:
        n = sum(1 for p in self.positions.values() if (p.opened_ts or "") >= since_ts)
        n += sum(1 for c in self.closed if (c.opened_ts or "") >= since_ts)
        return n

    def invested_cost(self) -> float:
        return sum(p.cost_usd for p in self.positions.values())


def is_close_row(row: dict) -> bool:
    if str(row.get("action") or "CLOSE").upper() != "CLOSE":
        return False
    return str(row.get("status") or "").lower() not in FAILED_SELL


def build_book(venue: str, fills: list[dict], closes: list[dict]) -> Book:
    """fills rows: {ts, pair_id, wsname, qty, cost_usd}; closes rows: {ts, pair_id, proceeds_usd, rule_fired}."""
    events = []
    for r in fills:
        events.append((str(r.get("ts") or ""), 0, r))
    for r in closes:
        if is_close_row(r):
            events.append((str(r.get("ts") or ""), 1, r))
    events.sort(key=lambda e: (e[0], e[1]))
    positions: dict = {}
    closed: list = []
    last_close: dict = {}
    for ts, kind, row in events:
        pid = row["pair_id"]
        if kind == 0:
            qty = float(row.get("qty") or 0)
            if qty <= 0:
                continue
            pos = positions.get(pid)
            if pos is None:
                pos = positions[pid] = Position(pair_id=pid, wsname=row.get("wsname") or "", venue=venue, opened_ts=ts)
            pos.qty += qty
            pos.cost_usd += float(row.get("cost_usd") or 0)
            pos.n_buys += 1
            pos.last_fill_ts = ts
            pos.wsname = pos.wsname or row.get("wsname") or ""
            pos.fills.append(row)
            continue
        last_close[pid] = ts
        pos = positions.pop(pid, None)
        if pos is None:
            continue
        closed.append(ClosedEpisode(pair_id=pid, wsname=pos.wsname, opened_ts=pos.opened_ts, closed_ts=ts,
                                    cost_usd=pos.cost_usd, proceeds_usd=float(row.get("proceeds_usd") or 0),
                                    n_buys=pos.n_buys, rule_fired=row.get("rule_fired"),
                                    fee_usd=float(row.get("fee_usd") or 0)))
    return Book(venue=venue, positions=positions, closed=closed, last_close_ts=last_close)


def load_book(journal, venue: str) -> Book:
    book = build_book(venue, journal.fills(venue), journal.closes(venue))
    for pos in book.positions.values():
        st = journal.current_stop(venue, pos.pair_id, pos.opened_ts)
        if st:
            pos.stop_price = float(st["stop_price"])
            pos.stop_source = st["source"]
    return book


def stops_map(book: Book) -> dict:
    return {p.pair_id: p.stop_price for p in book.positions.values() if p.stop_price is not None}


# ---------------------------------------------------------------- reconcile (ported)

def asset_pair_index(universe_pairs: list[dict]) -> dict:
    out: dict = {}
    for pair in universe_pairs or []:
        base = str(pair.get("base") or "").upper()
        if base:
            out.setdefault(base, pair)
    return out


def reconcile(book: Book, leftover_assets: list[dict], universe_pairs: list[dict], lasts: dict | None = None,
              dust_usd: float = 1.0, tolerance: float = 0.02) -> dict:
    lasts = lasts or {}
    by_asset = asset_pair_index(universe_pairs)
    held: dict = {}
    untracked, unmapped = [], []
    for row in leftover_assets:
        asset = str(row.get("asset") or "").upper()
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue
        pair = by_asset.get(asset)
        if not pair:
            unmapped.append({"asset": asset, "amount": amount})
            continue
        held[pair["pair_id"]] = held.get(pair["pair_id"], 0.0) + amount
    for pid, amount in sorted(held.items()):
        last = lasts.get(pid)
        if last is not None and amount * last < dust_usd:
            continue
        if book.position(pid) is not None:
            continue
        pair = next((p for p in universe_pairs if p["pair_id"] == pid), {})
        untracked.append({"pair_id": pid, "wsname": pair.get("wsname") or "", "qty": amount, "last": last})
    missing = []
    for pos in book.open_positions():
        have = held.get(pos.pair_id, 0.0)
        if have < pos.qty * (1.0 - tolerance):
            missing.append({"pair_id": pos.pair_id, "book_qty": pos.qty, "kraken_qty": have})
    return {"ts": utc_now(), "untracked": untracked, "missing": missing, "unmapped": unmapped,
            "clean": not untracked and not missing}


def paper_cash(journal, starting_usd: float) -> float:
    cash = float(starting_usd)
    for f in journal.fills("paper"):
        cash -= float(f["cost_usd"]) + float(f["fee_usd"] or 0)
    for c in journal.closes("paper"):
        if is_close_row(c):
            cash += float(c["proceeds_usd"] or 0) - float(c["fee_usd"] or 0)
    return cash

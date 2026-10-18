#!/usr/bin/python3 -I
"""Position book from ledger fills and closes. One open episode per pair.

An episode opens on the first fill while the pair is flat and ends on a CLOSE
row. Entry, peak window and buy count reset with every new episode, so a pair
bought again after a full close is a new position, not the old one.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from desk_paths import ledger_dir

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
    source: str = "ledger"
    fills: list[dict] = field(default_factory=list)

    @property
    def entry(self) -> float:
        return self.cost_usd / self.qty if self.qty > 0 else 0.0

    def to_dict(self, with_fills: bool = False) -> dict:
        row = asdict(self)
        if not with_fills:
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

    @property
    def pnl_usd(self) -> float:
        return self.proceeds_usd - self.cost_usd


@dataclass
class Book:
    venue: str
    positions: dict[str, Position]
    closed: list[ClosedEpisode]
    last_close_ts: dict[str, str]
    ledger: Path | None = None

    def open_positions(self) -> list[Position]:
        return sorted(self.positions.values(), key=lambda p: p.opened_ts or "")

    def position(self, pair_id: str) -> Position | None:
        return self.positions.get(pair_id)

    def opened_since(self, since_ts: str) -> int:
        """Episodes (open or closed) that started at or after since_ts."""
        n = sum(1 for p in self.positions.values() if (p.opened_ts or "") >= since_ts)
        n += sum(1 for c in self.closed if (c.opened_ts or "") >= since_ts)
        return n


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def is_close_row(row: dict) -> bool:
    """Only an executed CLOSE ends an episode. HOLD and SKIP_DUST notes do not."""
    if str(row.get("action") or "").upper() != "CLOSE":
        return False
    return str(row.get("status") or "").lower() not in FAILED_SELL


def load_book(venue: str = "kraken", ledger: Path | None = None) -> Book:
    ledger = ledger or ledger_dir()
    events: list[tuple[str, int, dict]] = []
    for row in load_jsonl(ledger / "fills.jsonl"):
        if row.get("pair_id") and (row.get("venue") or "paper") == venue:
            events.append((str(row.get("ts") or ""), 0, row))
    for row in load_jsonl(ledger / "closes.jsonl"):
        if row.get("pair_id") and (row.get("venue") or "paper") == venue and is_close_row(row):
            events.append((str(row.get("ts") or ""), 1, row))
    events.sort(key=lambda e: (e[0], e[1]))

    positions: dict[str, Position] = {}
    closed: list[ClosedEpisode] = []
    last_close: dict[str, str] = {}
    for ts, kind, row in events:
        pid = row["pair_id"]
        if kind == 0:
            qty = float(row.get("volume_base") or 0)
            if qty <= 0:
                continue
            pos = positions.get(pid)
            if pos is None:
                pos = positions[pid] = Position(
                    pair_id=pid,
                    wsname=row.get("wsname") or "",
                    venue=venue,
                    opened_ts=ts,
                )
            pos.qty += qty
            pos.cost_usd += float(row.get("filled") or 0)
            pos.n_buys += 1
            pos.last_fill_ts = ts
            pos.wsname = pos.wsname or row.get("wsname") or ""
            pos.fills.append(row)
            continue
        last_close[pid] = ts
        pos = positions.pop(pid, None)
        if pos is None:
            continue
        closed.append(
            ClosedEpisode(
                pair_id=pid,
                wsname=pos.wsname,
                opened_ts=pos.opened_ts,
                closed_ts=ts,
                cost_usd=pos.cost_usd,
                proceeds_usd=float(row.get("filled") or 0),
                n_buys=pos.n_buys,
                rule_fired=row.get("rule_fired"),
            )
        )
    return Book(venue=venue, positions=positions, closed=closed, last_close_ts=last_close, ledger=ledger)


def asset_pair_index(universe: dict) -> dict[str, dict]:
    """Kraken balance asset code to its USD pair."""
    out: dict[str, dict] = {}
    for pair in universe.get("pairs") or []:
        base = str(pair.get("base") or "").upper()
        if base:
            out.setdefault(base, pair)
    return out


def reconcile(
    book: Book,
    leftover_assets: list[dict],
    universe: dict,
    lasts: dict[str, float] | None = None,
    dust_usd: float = 1.0,
    tolerance: float = 0.02,
) -> dict:
    """Compare ledger positions with Kraken balances.

    untracked: Kraken holds a non-dust asset the book does not. It is still
    managed, with entry from the last ledger fill for that pair.
    missing: the book holds a pair Kraken does not (or holds much less of).
    """
    lasts = lasts or {}
    by_asset = asset_pair_index(universe)
    held: dict[str, float] = {}
    untracked: list[dict] = []
    unmapped: list[dict] = []
    for row in leftover_assets:
        asset = str(row.get("asset") or "").upper()
        amount = float(row.get("amount") or 0)
        if amount <= 0:
            continue
        pair = by_asset.get(asset)
        if not pair:
            unmapped.append({"asset": asset, "amount": amount})
            continue
        pid = pair["pair_id"]
        held[pid] = held.get(pid, 0.0) + amount
    for pid, amount in sorted(held.items()):
        last = lasts.get(pid)
        if last is not None and amount * last < dust_usd:
            continue
        if book.position(pid) is not None:
            continue
        pair = next((p for p in universe.get("pairs") or [] if p["pair_id"] == pid), {})
        untracked.append(
            {
                "pair_id": pid,
                "wsname": pair.get("wsname") or "",
                "qty": amount,
                "entry": last_fill_price(pid, book.venue, book.ledger),
                "last": last,
            }
        )
    missing = []
    for pos in book.open_positions():
        have = held.get(pos.pair_id, 0.0)
        if have < pos.qty * (1.0 - tolerance):
            missing.append({"pair_id": pos.pair_id, "book_qty": pos.qty, "kraken_qty": have})
    return {
        "ts": utc_now(),
        "untracked": untracked,
        "missing": missing,
        "unmapped": unmapped,
    }


def last_fill_price(pair_id: str, venue: str = "kraken", ledger: Path | None = None) -> float | None:
    ledger = ledger or ledger_dir()
    price = None
    for row in load_jsonl(ledger / "fills.jsonl"):
        if row.get("pair_id") == pair_id and (row.get("venue") or "paper") == venue:
            qty = float(row.get("volume_base") or 0)
            if qty > 0:
                price = float(row.get("filled") or 0) / qty
    return price


def reconcile_path() -> Path:
    return ledger_dir() / "reconcile.json"


def write_reconcile(payload: dict) -> None:
    path = reconcile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_reconcile() -> dict:
    path = reconcile_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def managed_positions(book: Book) -> list[Position]:
    """Ledger positions plus untracked Kraken holdings from the last reconcile."""
    out = book.open_positions()
    rec = load_reconcile()
    for row in rec.get("untracked") or []:
        pid = row.get("pair_id")
        entry = row.get("entry")
        qty = float(row.get("qty") or 0)
        if not pid or not entry or qty <= 0 or book.position(pid) is not None:
            continue
        out.append(
            Position(
                pair_id=pid,
                wsname=row.get("wsname") or "",
                venue=book.venue,
                qty=qty,
                cost_usd=float(entry) * qty,
                opened_ts=rec.get("ts"),
                source="reconcile",
            )
        )
    return out


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    b = load_book()
    print(
        json.dumps(
            {
                "venue": b.venue,
                "opens": [p.to_dict() for p in b.open_positions()],
                "closed": len(b.closed),
                "realised_pnl_usd": round(sum(c.pnl_usd for c in b.closed), 2),
            },
            indent=2,
        )
    )

"""Paper broker: fills simulated against the live ticker.

Buy (limit IOC): fills at ask * (1 + harness.paper.slippageBps) if that is at or under
the limit, else unfilled. Volume is rounded like Kraken (lot_decimals, round down).
Sell (market): fills at bid * (1 - slippageBps). Fee = fee.rate of notional.
Cash = harness.paper.startingUsd - buys - fees + sells, derived from the journal.
"""
from __future__ import annotations

from ..book import load_book, paper_cash
from ..market.kraken_public import round_base
from ..util import utc_now
from .base import Broker


class PaperBroker(Broker):
    venue = "paper"

    def __init__(self, cfg: dict, journal, market):
        self.cfg = cfg
        self.journal = journal
        self.market = market
        h = cfg.get("harness") or {}
        self.slip = float((h.get("paper") or {}).get("slippageBps", 10)) / 10000.0
        self.fee = float((cfg.get("fee") or {}).get("rate") or 0.004)
        self.starting = float((h.get("paper") or {}).get("startingUsd") or 0)
        self._seq = 0

    def _txid(self, side: str) -> str:
        self._seq += 1
        return f"PAPER-{side[0].upper()}-{utc_now()}-{self._seq}"

    def balance(self) -> dict:
        cash = paper_cash(self.journal, self.starting)
        book = load_book(self.journal, "paper")
        pids = [p.pair_id for p in book.open_positions()]
        ticks = self.market.tickers(pids) if pids else {}
        marked = 0.0
        for p in book.open_positions():
            last = (ticks.get(p.pair_id) or {}).get("last")
            marked += p.qty * float(last) if last else p.cost_usd
        return {"usd_spot": round(cash, 2), "equity_usd": round(cash + marked, 2),
                "leftover_assets": [{"asset": p.pair_id, "amount": p.qty} for p in book.open_positions()],
                "source": "paper"}

    def buy_limit_ioc(self, pair: dict, usd: float, limit_price: float, cycle_id: str) -> dict:
        tick = self.market.tickers([pair["pair_id"]]).get(pair["pair_id"]) or {}
        ask = tick.get("ask")
        order = {"pair_id": pair["pair_id"], "side": "buy", "ordertype": "limit", "price": float(limit_price),
                 "oflags": (self.cfg.get("live") or {}).get("buyOflags") or "fciq", "timeinforce": "IOC"}
        if not ask:
            return {"status": "unfilled", "qty": 0.0, "cost_usd": 0.0, "order": {**order, "status": "no_ask"}}
        fill_px = float(ask) * (1.0 + self.slip)
        volume = float(round_base(usd / float(limit_price), pair.get("lot_decimals")))
        order["volume"] = volume
        if fill_px > float(limit_price) or volume <= 0 or volume < float(pair.get("ordermin") or 0):
            return {"status": "unfilled", "qty": 0.0, "cost_usd": 0.0, "order": {**order, "status": "expired"}}
        cost = volume * fill_px
        txid = self._txid("buy")
        return {"status": "filled", "qty": volume, "cost_usd": round(cost, 6), "price": fill_px,
                "fee_usd": round(cost * self.fee, 6), "txid": txid, "order": {**order, "status": "closed", "txid": txid}}

    def sell_market(self, pair: dict, qty: float, cycle_id: str) -> dict:
        tick = self.market.tickers([pair["pair_id"]]).get(pair["pair_id"]) or {}
        bid = tick.get("bid")
        volume = float(round_base(qty, pair.get("lot_decimals")))
        order = {"pair_id": pair["pair_id"], "side": "sell", "ordertype": "market", "volume": volume, "oflags": "fciq"}
        if not bid:
            return {"status": "failed", "qty": 0.0, "proceeds_usd": 0.0, "order": {**order, "status": "no_bid"}}
        px = float(bid) * (1.0 - self.slip)
        proceeds = volume * px
        txid = self._txid("sell")
        return {"status": "closed", "qty": volume, "proceeds_usd": round(proceeds, 6), "price": px,
                "fee_usd": round(proceeds * self.fee, 6), "txid": txid, "order": {**order, "status": "closed", "txid": txid}}

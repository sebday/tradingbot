"""Live Kraken broker. The only harness code that reads `pass`.

Buys: limit IOC at the gate's limit price with live.buyOflags ("fciq").
Sells: market with oflags "fciq", as scripts/kraken_execute.py does today.
Orders refuse unless the broker was built with orders allowed, which only
harness/__main__.py does, and only when harness.mode == "live" (or exits.liveKraken after cutover). balance() is query-only.
"""
from __future__ import annotations

import time

from ..market.kraken_public import round_base, round_price
from . import kraken_private as kp
from .base import Broker, BrokerError


class KrakenBroker(Broker):
    venue = "kraken"

    def __init__(self, cfg: dict, allow_orders: bool = False):
        self.cfg = cfg
        self.allow_orders = bool(allow_orders)

    def _keys(self):
        return kp.load_keys(self.cfg)

    def balance(self) -> dict:
        key, secret = self._keys()
        try:
            raw = kp.private_post("Balance", key, secret)
            trade = kp.private_post("TradeBalance", key, secret, {"asset": "ZUSD"})
        finally:
            del key, secret
        usd, leftover = kp.split_balances(raw)
        try:
            equity = float(trade.get("eb"))
        except (TypeError, ValueError):
            equity = usd
        return {"usd_spot": round(usd, 2), "equity_usd": round(equity, 2), "leftover_assets": leftover,
                "source": "kraken"}

    def _wait(self, key, secret, txid) -> dict:
        last: dict = {}
        for _ in range(8):
            time.sleep(0.4)
            last = kp.query_orders(key, secret, txid)
            info = last.get(txid) or (next(iter(last.values())) if last else {})
            if (info.get("status") or "").lower() in {"closed", "canceled", "expired"}:
                return info
        return last.get(txid) or (next(iter(last.values())) if last else {})

    def _require_orders(self):
        if not self.allow_orders:
            raise BrokerError("kraken broker built without allow_orders; refusing AddOrder")

    def buy_limit_ioc(self, pair: dict, usd: float, limit_price: float, cycle_id: str) -> dict:
        self._require_orders()
        price = round_price(limit_price, pair.get("pair_decimals"))
        volume = round_base(usd / float(price), pair.get("lot_decimals"))
        if float(volume) < float(pair.get("ordermin") or 0):
            return {"status": "rejected", "error": "below ordermin", "qty": 0.0, "cost_usd": 0.0,
                    "order": {"price": float(price), "volume": float(volume)}}
        oflags = (self.cfg.get("live") or {}).get("buyOflags") or "fciq"
        order = {"pair_id": pair["pair_id"], "side": "buy", "ordertype": "limit", "price": float(price),
                 "volume": float(volume), "oflags": oflags, "timeinforce": "IOC"}
        key, secret = self._keys()
        try:
            result = kp.add_order(key, secret, pair=pair["pair_id"], side="buy", volume=volume, oflags=oflags,
                                  ordertype="limit", price=price, timeinforce="IOC")
            txid = (result.get("txid") or [None])[0]
            info = self._wait(key, secret, txid) if txid else {}
        finally:
            del key, secret
        vol_exec = float(info.get("vol_exec") or 0)
        order.update(txid=txid, status=info.get("status"), descr=(result.get("descr") or {}).get("order"))
        if vol_exec <= 0:
            return {"status": "unfilled", "qty": 0.0, "cost_usd": 0.0, "txid": txid, "order": order}
        fill_px = float(info.get("price") or 0) or float(price)
        return {"status": "filled", "qty": vol_exec, "cost_usd": float(info.get("cost") or vol_exec * fill_px),
                "price": fill_px, "fee_usd": float(info.get("fee") or 0), "txid": txid, "order": order}

    def sell_market(self, pair: dict, qty: float, cycle_id: str) -> dict:
        self._require_orders()
        volume = round_base(qty, pair.get("lot_decimals"))
        order = {"pair_id": pair["pair_id"], "side": "sell", "ordertype": "market", "volume": float(volume),
                 "oflags": "fciq", "timeinforce": None}
        key, secret = self._keys()
        try:
            result = kp.add_order(key, secret, pair=pair["pair_id"], side="sell", volume=volume, oflags="fciq")
            txid = (result.get("txid") or [None])[0]
            info = self._wait(key, secret, txid) if txid else {}
        finally:
            del key, secret
        status = str(info.get("status") or "").lower()
        vol_exec = float(info.get("vol_exec") or 0)
        executed = bool(txid) and (vol_exec > 0 or status in {"open", "pending", ""})
        order.update(txid=txid, status=info.get("status"))
        return {"status": "closed" if executed else "failed", "qty": vol_exec or float(volume),
                "proceeds_usd": float(info.get("cost") or 0), "price": float(info.get("price") or 0),
                "fee_usd": float(info.get("fee") or 0), "txid": txid, "order": order}

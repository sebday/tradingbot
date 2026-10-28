"""Kraken hands. Keys stay here. With desk.live false, orders are recorded and not sent."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

from .config import ROOT, live as live_mode, load_config, parse_ts, read_halt, utc_now

DUST_USD = 1.0


def _scripts():
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import desk_book
    import desk_halt
    import kraken_private as kp
    import kraken_universe as ku

    return desk_book, desk_halt, kp, ku


def _order_lock():
    rd = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(rd) if rd else Path.home() / ".cache"
    path = base / "evo-trading-sell.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = path.open("a", encoding="utf-8")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class Services:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.live = live_mode(self.cfg)

    def entries_blocked(self) -> bool:
        if read_halt():
            return True
        _book, halt, _kp, _ku = _scripts()
        try:
            return bool(halt.is_halted())
        except Exception:
            return False

    def positions(self) -> list:
        book, _halt, _kp, _ku = _scripts()
        loaded = book.load_book("kraken")
        return book.managed_positions(loaded)

    def pair(self, pair_id: str) -> dict | None:
        _book, _halt, _kp, ku = _scripts()
        return ku.pair_index(ku.load_universe()).get(pair_id)

    def tickers(self, pair_ids: list[str]) -> dict:
        if not pair_ids:
            return {}
        _book, _halt, _kp, ku = _scripts()
        return ku.fetch_tickers(pair_ids)

    def ohlc(self, pair_id: str) -> list:
        _book, _halt, _kp, ku = _scripts()
        return ku.fetch_ohlc(pair_id, 60)

    def cash(self) -> float:
        return float(self.balance().get("usd_spot") or 0)

    def balance(self) -> dict:
        _book, _halt, kp, _ku = _scripts()
        key, secret = kp.load_keys(self.cfg)
        try:
            raw = kp.fetch_balance(key, secret)
            trade = kp.fetch_trade_balance(key, secret, "ZUSD")
        finally:
            del key, secret
        usd, leftover = kp.split_balances(raw)
        try:
            equity = float(trade.get("eb"))
        except (TypeError, ValueError):
            equity = usd
        return {"usd_spot": round(usd, 2), "equity_usd": round(equity, 2), "leftover_assets": leftover}

    def cancel(self, txid: str) -> None:
        if not txid or not self.live:
            return
        _book, _halt, kp, _ku = _scripts()
        key, secret = kp.load_keys(self.cfg)
        try:
            kp.private_post("CancelOrder", key, secret, {"txid": txid})
        except Exception:
            pass
        finally:
            del key, secret

    def query(self, txid: str) -> dict:
        if not txid or not self.live:
            return {}
        _book, _halt, kp, _ku = _scripts()
        key, secret = kp.load_keys(self.cfg)
        try:
            found = kp.query_orders(key, secret, txid)
        finally:
            del key, secret
        return found.get(txid) or (next(iter(found.values())) if found else {})

    def place_resting(self, entry: dict) -> dict:
        if not self.live:
            return {"ok": True, "shadow": True}
        pair = self.pair(entry["pair_id"])
        if not pair:
            return {"ok": False, "error": "pair not in universe"}
        params = json.loads(entry["params_json"])
        price = float(params["price"])
        _book, _halt, kp, ku = _scripts()
        volume = ku.round_base(float(entry["size_usd"]) / price, pair.get("lot_decimals"))
        if float(volume) <= 0 or float(volume) < float(pair.get("ordermin") or 0):
            return {"ok": False, "error": "below ordermin"}
        extra = {
            "pair": entry["pair_id"],
            "type": "buy",
            "ordertype": "limit" if entry["kind"] == "buy_limit" else "stop-loss",
            "volume": volume,
            "price": ku.round_price(price, pair.get("pair_decimals")),
            "oflags": (self.cfg.get("live") or {}).get("buyOflags") or "fciq",
        }
        expires = parse_ts(entry.get("expires_at"))
        if expires:
            extra["expiretm"] = str(int(expires))
        key, secret = kp.load_keys(self.cfg)
        try:
            result = kp.private_post("AddOrder", key, secret, extra)
        finally:
            del key, secret
        txids = result.get("txid") or []
        return {"ok": True, "txid": txids[0] if txids else None, "descr": (result.get("descr") or {}).get("order")}

    def buy(self, entry: dict, ask: float) -> dict:
        """Immediate buy. Shadow when desk.live is false."""
        pair = self.pair(entry["pair_id"]) or {"pair_id": entry["pair_id"], "wsname": entry.get("wsname") or ""}
        if not self.live:
            return {
                "ok": True, "live": False, "status": "shadow", "qty": None, "price": float(ask),
                "cost_usd": float(entry["size_usd"]), "txid": None, "opened_ts": utc_now(),
                "wsname": pair.get("wsname") or entry.get("wsname") or "",
            }
        return self._buy_live(entry, pair, float(ask))

    def sell(self, pos, reason: str, rule_name: str) -> dict:
        if not self.live:
            return {
                "ok": True, "live": False, "status": "shadow", "qty": pos.qty, "price": None,
                "proceeds_usd": None, "txid": None,
            }
        return self._sell_live(pos, rule_name)

    def note_fill(self, entry: dict, fill: dict) -> None:
        """A resting order already filled on Kraken. Record it in the legacy ledger."""
        if not self.live or not fill.get("ok"):
            return
        pair = self.pair(entry["pair_id"]) or {}
        _append_fill(self.cfg, {
            "pair_id": entry["pair_id"],
            "wsname": pair.get("wsname") or entry.get("wsname") or "",
            "requested": float(entry["size_usd"]),
            "filled": float(fill.get("proceeds_usd") or entry["size_usd"]),
            "fill_price": fill.get("price"),
            "volume_base": fill.get("qty"),
            "txid": fill.get("txid"),
            "order_type": entry["kind"],
        })

    def _buy_live(self, entry: dict, pair: dict, ask: float) -> dict:
        _book, _halt, kp, ku = _scripts()
        bps = float((self.cfg.get("entry") or {}).get("maxSlippageBps", 40))
        limit = ask * (1.0 + bps / 10000.0)
        price = ku.round_price(limit, pair.get("pair_decimals"))
        volume = ku.round_base(float(entry["size_usd"]) / float(price), pair.get("lot_decimals"))
        if float(volume) <= 0 or float(volume) < float(pair.get("ordermin") or 0):
            return {"ok": False, "live": True, "error": "below ordermin", "status": "rejected"}
        lock = _order_lock()
        try:
            key, secret = kp.load_keys(self.cfg)
            try:
                result = kp.add_order(
                    key, secret, pair=entry["pair_id"], side="buy", volume=volume,
                    oflags=(self.cfg.get("live") or {}).get("buyOflags") or "fciq",
                    ordertype="limit", price=price, timeinforce="IOC",
                )
                txids = result.get("txid") or []
                txid = txids[0] if txids else None
                info = _wait_order(kp, key, secret, txid) if txid else {}
            finally:
                del key, secret
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        vol = float(info.get("vol_exec") or 0)
        if vol <= 0:
            return {"ok": False, "live": True, "status": "unfilled", "error": "not filled", "txid": txid}
        cost = float(info.get("cost") or 0) or vol * float(info.get("price") or price)
        ts = utc_now()
        _append_fill(self.cfg, {
            "pair_id": entry["pair_id"],
            "wsname": pair.get("wsname") or "",
            "requested": float(entry["size_usd"]),
            "filled": cost,
            "fill_price": float(info.get("price") or price),
            "volume_base": vol,
            "txid": txid,
            "order_type": "limit_ioc",
            "fee_usd": float(info.get("fee") or 0),
            "ts": ts,
        })
        return {
            "ok": True, "live": True, "status": "filled", "qty": vol,
            "price": float(info.get("price") or price), "cost_usd": cost, "txid": txid,
            "opened_ts": ts, "wsname": pair.get("wsname") or "",
        }

    def _sell_live(self, pos, rule_name: str) -> dict:
        _book, _halt, kp, ku = _scripts()
        pair = self.pair(pos.pair_id) or {}
        ticks = self.tickers([pos.pair_id])
        last = (ticks.get(pos.pair_id) or {}).get("last")
        if last and pos.qty * float(last) < DUST_USD:
            return {"ok": True, "live": True, "status": "dust", "qty": pos.qty, "price": float(last)}
        lock = _order_lock()
        try:
            fresh = self.positions()
            pos = next((p for p in fresh if p.pair_id == pos.pair_id), None)
            if pos is None:
                return {"ok": True, "live": True, "status": "already_closed", "qty": 0}
            key, secret = kp.load_keys(self.cfg)
            try:
                volume = ku.round_base(pos.qty, pair.get("lot_decimals"))
                result = kp.add_order(
                    key, secret, pair=pos.pair_id, side="sell", volume=volume, oflags="fciq",
                )
                txids = result.get("txid") or []
                txid = txids[0] if txids else None
                info = _wait_order(kp, key, secret, txid) if txid else {}
            finally:
                del key, secret
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        vol = float(info.get("vol_exec") or 0)
        status = str(info.get("status") or "").lower()
        executed = bool(txid) and (vol > 0 or status in {"open", "pending", ""})
        if not executed:
            return {"ok": False, "live": True, "status": "failed", "error": "sell did not fill", "txid": txid}
        _append_close(pos, rule_name, txid, info, vol)
        return {
            "ok": True, "live": True, "status": "filled", "qty": vol or pos.qty,
            "price": float(info.get("price") or 0) or None,
            "proceeds_usd": float(info.get("cost") or 0) or None, "txid": txid,
        }


def _wait_order(kp, key: str, secret: str, txid: str) -> dict:
    last: dict = {}
    for _ in range(8):
        time.sleep(0.4)
        found = kp.query_orders(key, secret, txid)
        info = found.get(txid) or (next(iter(found.values())) if found else {})
        last = info
        if str(info.get("status") or "").lower() in {"closed", "canceled", "expired"}:
            return info
    return last


def _ledger(name: str) -> Path:
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from desk_paths import ledger_dir

    path = ledger_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_fill(cfg: dict, row: dict) -> None:
    payload = {
        "ts": row.get("ts") or utc_now(),
        "cycle_id": "desk",
        "pair_id": row["pair_id"],
        "wsname": row.get("wsname") or "",
        "requested": round(float(row.get("requested") or 0), 2),
        "filled": round(float(row.get("filled") or 0), 2),
        "fill_price": row.get("fill_price"),
        "order_type": row.get("order_type"),
        "fee_usd": float(row.get("fee_usd") or 0),
        "venue": "kraken",
        "txid": row.get("txid"),
        "volume_base": row.get("volume_base"),
    }
    with _ledger("fills.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _append_close(pos, rule_name: str, txid: str | None, info: dict, vol: float) -> None:
    payload = {
        "ts": utc_now(),
        "cycle_id": "desk",
        "pair_id": pos.pair_id,
        "wsname": pos.wsname,
        "action": "CLOSE",
        "rule_fired": rule_name,
        "venue": "kraken",
        "txid": txid,
        "entry": pos.entry,
        "opened_ts": pos.opened_ts,
        "fill_price": float(info.get("price") or 0),
        "filled": float(info.get("cost") or 0),
        "volume_base": vol or pos.qty,
        "status": info.get("status") or "closed",
    }
    with _ledger("closes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")

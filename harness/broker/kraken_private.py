"""Kraken private REST, ported from scripts/kraken_private.py. Keys from `pass`, never printed.
Only harness/broker/kraken.py imports this module (doctor enforces it)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT_UA = "trading-desk-harness/0.1"
KRAKEN_PRIVATE = "https://api.kraken.com"
USD_CASH = {"ZUSD", "USD"}

_last_nonce = 0


def next_nonce() -> str:
    global _last_nonce
    n = int(time.time() * 1000)
    if n <= _last_nonce:
        n = _last_nonce + 1
    _last_nonce = n
    return str(n)


def pass_show(entry: str) -> str:
    run = subprocess.run(["pass", "show", entry], check=True, capture_output=True, text=True)
    line = (run.stdout or "").splitlines()[0] if run.stdout else ""
    if not line.strip():
        raise RuntimeError(f"empty pass entry {entry}")
    return line.strip()


def load_keys(cfg: dict) -> tuple[str, str]:
    kraken = cfg.get("kraken") or {}
    return pass_show(kraken["passKey"]), pass_show(kraken["passSecret"])


def sign(urlpath: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (data["nonce"] + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def private_post(path: str, key: str, secret: str, extra: dict | None = None) -> dict:
    urlpath = f"/0/private/{path}"
    data = {"nonce": next_nonce()}
    if extra:
        data.update(extra)
    req = urllib.request.Request(
        KRAKEN_PRIVATE + urlpath, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"API-Key": key, "API-Sign": sign(urlpath, data, secret), "User-Agent": ROOT_UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"kraken {path} http {exc.code}: {body[:300]}") from exc
    errors = payload.get("error") or []
    if errors:
        raise RuntimeError(f"kraken {path}: {errors}")
    result = payload.get("result")
    if result is None:
        raise RuntimeError(f"kraken {path}: empty result")
    return result


def add_order(key, secret, *, pair, side, volume, oflags=None, validate=False, ordertype="market", price=None,
              timeinforce=None) -> dict:
    extra = {"pair": pair, "type": side, "ordertype": ordertype, "volume": volume}
    if ordertype == "limit":
        if not price:
            raise ValueError("limit order needs a price")
        extra["price"] = price
    if timeinforce:
        extra["timeinforce"] = timeinforce
    if oflags:
        extra["oflags"] = oflags
    if validate:
        extra["validate"] = "true"
    return private_post("AddOrder", key, secret, extra)


def query_orders(key, secret, txid) -> dict:
    return private_post("QueryOrders", key, secret, {"txid": txid})


def split_balances(raw: dict) -> tuple[float, list[dict]]:
    usd, leftover = 0.0, []
    for asset, amount in sorted(raw.items()):
        qty = float(amount)
        if qty == 0:
            continue
        if asset.upper() in USD_CASH:
            usd += qty
        else:
            leftover.append({"asset": asset, "amount": qty})
    return usd, leftover

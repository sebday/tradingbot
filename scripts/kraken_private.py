#!/usr/bin/env python3
"""Kraken private REST: load keys from pass, never print them."""
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
from typing import Any

ROOT_UA = "trading-desk/0.3"
KRAKEN_PRIVATE = "https://api.kraken.com"

USD_CASH = {"ZUSD", "USD"}
BTC_ASSETS = {"XXBT", "XBT", "BTC"}

_last_nonce = 0


def next_nonce() -> str:
    global _last_nonce
    n = int(time.time() * 1000)
    if n <= _last_nonce:
        n = _last_nonce + 1
    _last_nonce = n
    return str(n)


def pass_show(entry: str) -> str:
    run = subprocess.run(
        ["pass", "show", entry],
        check=True,
        capture_output=True,
        text=True,
    )
    line = (run.stdout or "").splitlines()[0] if run.stdout else ""
    if not line.strip():
        raise RuntimeError(f"empty pass entry {entry}")
    return line.strip()


def load_keys(cfg: dict) -> tuple[str, str]:
    kraken = cfg.get("kraken") or {}
    key_entry = kraken.get("passKey") or "omarchy/kraken/api-key"
    secret_entry = kraken.get("passSecret") or "omarchy/kraken/api-secret"
    return pass_show(key_entry), pass_show(secret_entry)


def sign(urlpath: str, data: dict[str, str], secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (data["nonce"] + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def private_post(path: str, key: str, secret: str, extra: dict[str, str] | None = None) -> dict:
    urlpath = f"/0/private/{path}"
    data = {"nonce": next_nonce()}
    if extra:
        data.update(extra)
    postdata = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        KRAKEN_PRIVATE + urlpath,
        data=postdata,
        method="POST",
        headers={
            "API-Key": key,
            "API-Sign": sign(urlpath, data, secret),
            "User-Agent": ROOT_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
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


def fetch_balance(key: str, secret: str) -> dict[str, str]:
    return private_post("Balance", key, secret)


def fetch_trade_balance(key: str, secret: str, asset: str = "ZUSD") -> dict[str, Any]:
    return private_post("TradeBalance", key, secret, {"asset": asset})


def add_order(
    key: str,
    secret: str,
    *,
    pair: str,
    side: str,
    volume: str,
    oflags: str | None = None,
    validate: bool = False,
) -> dict[str, Any]:
    extra = {
        "pair": pair,
        "type": side,
        "ordertype": "market",
        "volume": volume,
    }
    if oflags:
        extra["oflags"] = oflags
    if validate:
        extra["validate"] = "true"
    return private_post("AddOrder", key, secret, extra)


def query_orders(key: str, secret: str, txid: str) -> dict[str, Any]:
    return private_post("QueryOrders", key, secret, {"txid": txid})


def split_balances(raw: dict[str, str]) -> tuple[float, list[dict]]:
    usd = 0.0
    leftover = []
    for asset, amount in sorted(raw.items()):
        qty = float(amount)
        if qty == 0:
            continue
        if asset.upper() in USD_CASH:
            usd += qty
        else:
            leftover.append({"asset": asset, "amount": qty})
    return usd, leftover


def btc_spot(leftover: list[dict]) -> float:
    total = 0.0
    for row in leftover:
        if row["asset"].upper() in BTC_ASSETS:
            total += float(row["amount"])
    return total

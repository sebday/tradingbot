"""One Broker interface for paper and Kraken. Brokers place orders and report what
happened; the executor (harness/executor.py) journals the result. Brokers never decide."""
from __future__ import annotations

from abc import ABC, abstractmethod

DUST_USD = 1.0


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    venue = "abstract"

    @abstractmethod
    def balance(self) -> dict:
        """{usd_spot, equity_usd, leftover_assets:[{asset, amount}], source}"""

    @abstractmethod
    def buy_limit_ioc(self, pair: dict, usd: float, limit_price: float, cycle_id: str) -> dict:
        """Limit IOC buy. -> {status: filled|unfilled, qty, cost_usd, price, fee_usd, txid, order:{...}}"""

    @abstractmethod
    def sell_market(self, pair: dict, qty: float, cycle_id: str) -> dict:
        """Market sell. -> {status: closed|failed, qty, proceeds_usd, price, fee_usd, txid, order:{...}}"""

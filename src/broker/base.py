"""
Broker-Adapter Abstract Base.

Definiert eine domain-agnostische Schnittstelle, die jeder Broker
(Alpaca, IBKR, Mock) implementieren muss. Damit ist das Trading-System
broker-blind.

Usage:
    from src.broker import get_broker
    broker = get_broker(config.broker_kind)   # 'alpaca_paper' | 'mock'
    account = broker.get_account()
    quote = broker.get_quote("NVDA")
    order = broker.place_order(...)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class AccountState:
    cash_eur:         float
    equity_eur:       float
    buying_power_eur: float
    cash_usd:         float = 0.0
    equity_usd:       float = 0.0
    buying_power_usd: float = 0.0
    fx_rate:          float = 0.0     # EUR per 1 USD zum Zeitpunkt der Messung
    currency:         str = "EUR"
    raw:              dict = field(default_factory=dict)


@dataclass
class BrokerPosition:
    ticker:        str
    qty:           float
    avg_price:     float    # in der Kontowaehrung (USD bei Alpaca-US)
    market_price:  float
    market_value_eur: float
    unrealized_pl_eur: float
    currency:      str = "USD"


@dataclass
class Quote:
    ticker:    str
    bid:       float
    ask:       float
    last:      float
    timestamp: str
    currency:  str = "USD"


@dataclass
class OrderResult:
    order_id:        str
    status:          str        # 'pending' | 'filled' | 'rejected' | 'cancelled' | 'partial'
    ticker:          str
    side:            str
    qty:             float
    filled_qty:      float = 0.0
    avg_fill_price:  Optional[float] = None
    submitted_at:    Optional[str] = None
    filled_at:       Optional[str] = None
    error:           Optional[str] = None
    raw:             dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────
# ABSTRACT INTERFACE
# ────────────────────────────────────────────────────────────
class BrokerAdapter(ABC):
    """Jeder Broker implementiert diese Methoden. Alle Mengen in EUR."""

    name: str = "abstract"
    is_paper: bool = True

    @abstractmethod
    def get_account(self) -> AccountState: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    def place_order(
        self,
        ticker:     str,
        side:       str,             # 'buy' | 'sell'
        qty:        float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        client_id:  Optional[str] = None,
    ) -> OrderResult: ...

    @abstractmethod
    def get_order(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def list_orders(self, status: str = "open") -> list[OrderResult]: ...

    # ── Convenience ─────────────────────────────────────────
    def is_live(self) -> bool:
        return not self.is_paper

    def __repr__(self) -> str:
        kind = "PAPER" if self.is_paper else "LIVE"
        return f"<{self.__class__.__name__} {kind}>"

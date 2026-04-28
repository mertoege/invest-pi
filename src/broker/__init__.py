"""
Broker-Layer.

Wahl-Helper:
    from src.broker import get_broker
    broker = get_broker(kind="alpaca_paper")  # oder "mock"
"""

from __future__ import annotations

from .base import (
    AccountState,
    BrokerAdapter,
    BrokerPosition,
    OrderResult,
    Quote,
)


def get_broker(kind: str = "mock", **kwargs) -> BrokerAdapter:
    """
    Factory. Lazy import damit alpaca-py nicht zwingend installiert sein muss
    fuer Mock-only-Tests.
    """
    kind = kind.lower().strip()
    if kind in ("mock", "paper_mock", "simulator"):
        from .mock import MockBroker
        return MockBroker(**kwargs)
    if kind in ("alpaca_paper", "alpaca"):
        from .alpaca import AlpacaPaperBroker
        return AlpacaPaperBroker(**kwargs)
    raise ValueError(f"Unbekannter Broker-Typ: {kind!r}. "
                     f"Verfuegbar: 'mock', 'alpaca_paper'.")


__all__ = [
    "AccountState",
    "BrokerAdapter",
    "BrokerPosition",
    "OrderResult",
    "Quote",
    "get_broker",
]

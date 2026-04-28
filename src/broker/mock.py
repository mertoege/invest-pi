"""
MockBroker — In-Memory Paper-Trading-Simulator.

Nutzbar fuer:
  - Smoke-Tests ohne Network
  - Lokale Strategie-Entwicklung ohne Alpaca-Account
  - Replay-Backtests (TODO Phase 6)

Preise werden via data_loader (yfinance-Cache) gezogen.
Default: 0% slippage, 0 fees, instant-fill (synchron).
USD/EUR-Conversion: feste Rate (config oder 0.92 default).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Optional

import yaml

from .base import (
    AccountState,
    BrokerAdapter,
    BrokerPosition,
    OrderResult,
    Quote,
)


def _eur_per_usd() -> float:
    """Sehr einfache FX. Spaeter via yfinance EURUSD=X."""
    return 0.92  # ~ April 2026


def _load_starting_capital() -> float:
    """Aus config.yaml / settings.trading.starting_paper_capital."""
    cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
    try:
        raw = yaml.safe_load(cfg_path.read_text())
        return float((raw or {}).get("settings", {}).get("trading", {}).get(
            "starting_paper_capital", 10000.0))
    except (FileNotFoundError, AttributeError, ValueError):
        return 10000.0


class MockBroker(BrokerAdapter):
    """Eine pure-Python Simulation, kein Network."""

    name = "mock"
    is_paper = True

    def __init__(self, starting_capital_eur: Optional[float] = None):
        self._cash_eur: float = (
            starting_capital_eur if starting_capital_eur is not None
            else _load_starting_capital()
        )
        # ticker -> {qty, avg_price_usd, opened_at}
        self._positions: dict[str, dict] = {}
        # order_id -> OrderResult
        self._orders: dict[str, OrderResult] = {}
        self._fx = _eur_per_usd()

    # ── private helpers ─────────────────────────────────────
    def _fetch_price(self, ticker: str) -> float:
        """Letzter Close aus dem yfinance-Cache. Lazy import damit Mock auch ohne yfinance laeuft."""
        try:
            from ..common.data_loader import get_prices
            prices = get_prices(ticker, period="1mo")
            return float(prices["close"].iloc[-1])
        except Exception:
            # Fallback fuer Tests ohne Network: deterministischer Pseudo-Preis
            base = (sum(ord(c) for c in ticker) % 200) + 50.0
            return base

    def _market_value_eur(self, ticker: str, qty: float) -> tuple[float, float]:
        """Returns (last_usd, market_value_eur)."""
        last = self._fetch_price(ticker)
        return last, last * qty * self._fx

    # ── BrokerAdapter ───────────────────────────────────────
    def get_account(self) -> AccountState:
        positions_value_eur = 0.0
        for ticker, pos in self._positions.items():
            _, mv_eur = self._market_value_eur(ticker, pos["qty"])
            positions_value_eur += mv_eur
        equity = self._cash_eur + positions_value_eur
        return AccountState(
            cash_eur=self._cash_eur,
            equity_eur=equity,
            buying_power_eur=self._cash_eur,
            raw={"mock": True, "positions_value_eur": positions_value_eur},
        )

    def get_positions(self) -> list[BrokerPosition]:
        result = []
        for ticker, pos in self._positions.items():
            last, mv_eur = self._market_value_eur(ticker, pos["qty"])
            avg_eur = pos["avg_price_usd"] * self._fx
            unrealized = mv_eur - (avg_eur * pos["qty"])
            result.append(BrokerPosition(
                ticker=ticker,
                qty=pos["qty"],
                avg_price=pos["avg_price_usd"],
                market_price=last,
                market_value_eur=mv_eur,
                unrealized_pl_eur=unrealized,
            ))
        return result

    def get_quote(self, ticker: str) -> Quote:
        last = self._fetch_price(ticker)
        return Quote(
            ticker=ticker,
            bid=last * 0.999,
            ask=last * 1.001,
            last=last,
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )

    def place_order(
        self,
        ticker:     str,
        side:       str,
        qty:        float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        client_id:  Optional[str] = None,
    ) -> OrderResult:
        if side not in ("buy", "sell"):
            return OrderResult(
                order_id="", status="rejected",
                ticker=ticker, side=side, qty=qty,
                error=f"invalid side: {side}",
            )

        order_id = client_id or f"mock-{uuid.uuid4().hex[:12]}"
        last_price = self._fetch_price(ticker)
        fill_price = limit_price or last_price
        cost_eur = fill_price * qty * self._fx
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

        if side == "buy":
            if cost_eur > self._cash_eur:
                result = OrderResult(
                    order_id=order_id, status="rejected",
                    ticker=ticker, side="buy", qty=qty,
                    error=f"insufficient cash: need {cost_eur:.2f} EUR, have {self._cash_eur:.2f}",
                )
                self._orders[order_id] = result
                return result
            self._cash_eur -= cost_eur
            existing = self._positions.get(ticker, {"qty": 0, "avg_price_usd": 0, "opened_at": now})
            new_qty = existing["qty"] + qty
            new_avg = (
                (existing["avg_price_usd"] * existing["qty"]) + (fill_price * qty)
            ) / new_qty if new_qty > 0 else 0
            self._positions[ticker] = {
                "qty": new_qty,
                "avg_price_usd": new_avg,
                "opened_at": existing["opened_at"],
            }
        else:  # sell
            existing = self._positions.get(ticker)
            if not existing or existing["qty"] < qty:
                result = OrderResult(
                    order_id=order_id, status="rejected",
                    ticker=ticker, side="sell", qty=qty,
                    error=f"insufficient position: have {existing['qty'] if existing else 0}, want to sell {qty}",
                )
                self._orders[order_id] = result
                return result
            self._cash_eur += cost_eur
            existing["qty"] -= qty
            if existing["qty"] <= 0:
                del self._positions[ticker]
            else:
                self._positions[ticker] = existing

        result = OrderResult(
            order_id=order_id,
            status="filled",
            ticker=ticker,
            side=side,
            qty=qty,
            filled_qty=qty,
            avg_fill_price=fill_price,
            submitted_at=now,
            filled_at=now,
        )
        self._orders[order_id] = result
        return result

    def get_order(self, order_id: str) -> OrderResult:
        order = self._orders.get(order_id)
        if not order:
            return OrderResult(
                order_id=order_id, status="rejected",
                ticker="", side="", qty=0,
                error="order not found",
            )
        return order

    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status == "pending":
            order.status = "cancelled"
            return True
        return False

    def list_orders(self, status: str = "open") -> list[OrderResult]:
        if status == "all":
            return list(self._orders.values())
        target = "pending" if status == "open" else status
        return [o for o in self._orders.values() if o.status == target]

    # ── Test-Helpers ────────────────────────────────────────
    def __repr__(self) -> str:
        return f"<MockBroker cash={self._cash_eur:.2f} EUR positions={len(self._positions)}>"

    def reset(self, capital_eur: Optional[float] = None) -> None:
        self.__init__(capital_eur or _load_starting_capital())

"""
AlpacaPaperBroker — Wrapper um alpaca-py SDK fuer das Paper-API.

Setup auf dem Pi:
    pip install alpaca-py>=0.30.0 --break-system-packages
    export ALPACA_API_KEY="..."
    export ALPACA_API_SECRET="..."

Falls SDK nicht installiert oder Keys fehlen: alle Methoden raisen mit
einer klaren Fehlermeldung. Der Import selber crasht NICHT — damit Tests
mit MockBroker auch ohne alpaca-py laufen.

Hinweis Echtgeld:
  Diese Klasse benutzt IMMER paper-api.alpaca.markets als base_url. Fuer
  Live-Trading muesste man einen separaten AlpacaLiveBroker schreiben +
  einen LIVE_TRADING=true env-Toggle aktivieren. Bewusst getrennt, damit
  niemand versehentlich mit Echtgeld traded.
"""

from __future__ import annotations

import os
from typing import Optional

from ..common.retry import api_retry
from .base import (
    AccountState,
    BrokerAdapter,
    BrokerPosition,
    OrderResult,
    Quote,
)


_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _eur_per_usd() -> float:
    from ..common.fx import eur_per_usd
    return eur_per_usd()


class AlpacaPaperBroker(BrokerAdapter):
    """Lazy-init: SDK + Keys werden erst beim ersten Methoden-Aufruf geprueft."""

    name = "alpaca_paper"
    is_paper = True

    def __init__(self, api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 base_url: Optional[str] = None):
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._api_secret = api_secret or os.environ.get("ALPACA_API_SECRET", "")
        self._base_url = base_url or _PAPER_BASE_URL
        self._client = None
        self._fx = _eur_per_usd()
        self._fx_ts = __import__("time").monotonic()

    def _refresh_fx_if_stale(self):
        """FX-Rate alle 30 Min auffrischen."""
        import time
        if time.monotonic() - self._fx_ts > 1800:
            try:
                self._fx = _eur_per_usd()
                self._fx_ts = time.monotonic()
            except Exception:
                pass  # alten Wert behalten

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key or not self._api_secret:
            raise RuntimeError(
                "AlpacaPaperBroker: ALPACA_API_KEY und ALPACA_API_SECRET muessen gesetzt sein "
                "(env oder Constructor-args). Aktuell sind beide leer — "
                "Paper-Account auf https://app.alpaca.markets/paper/dashboard/overview erstellen."
            )
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as e:
            raise RuntimeError(
                "alpaca-py nicht installiert. Pi-Setup:\n"
                "  pip install alpaca-py --break-system-packages\n"
                f"Original-Fehler: {e}"
            )
        self._client = TradingClient(
            api_key=self._api_key,
            secret_key=self._api_secret,
            paper=True,
        )
        return self._client

    # ── BrokerAdapter ──────────────────────────────────────
    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def get_account(self) -> AccountState:
        self._refresh_fx_if_stale()
        client = self._ensure_client()
        acc = client.get_account()
        cash_usd = float(acc.cash)
        equity_usd = float(acc.equity)
        buying_power_usd = float(acc.buying_power)
        return AccountState(
            cash_eur=cash_usd * self._fx,
            equity_eur=equity_usd * self._fx,
            buying_power_eur=buying_power_usd * self._fx,
            cash_usd=cash_usd,
            equity_usd=equity_usd,
            buying_power_usd=buying_power_usd,
            fx_rate=self._fx,
            raw={
                "currency": "USD",
                "cash_usd": cash_usd,
                "equity_usd": equity_usd,
                "alpaca_id": str(acc.id),
                "status": str(acc.status),
            },
        )

    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def get_positions(self) -> list[BrokerPosition]:
        client = self._ensure_client()
        positions = client.get_all_positions()
        result = []
        for p in positions:
            qty = float(p.qty)
            avg = float(p.avg_entry_price)
            last = float(p.current_price) if p.current_price else avg
            mv_eur = last * qty * self._fx
            unreal_eur = (last - avg) * qty * self._fx
            result.append(BrokerPosition(
                ticker=p.symbol,
                qty=qty,
                avg_price=avg,
                market_price=last,
                market_value_eur=mv_eur,
                unrealized_pl_eur=unreal_eur,
                currency="USD",
            ))
        return result

    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def get_quote(self, ticker: str) -> Quote:
        """
        Holt aktuellen Kurs. Versucht zuerst Alpaca Market Data API,
        Fallback auf yfinance-Cache wenn Alpaca fehlschlaegt.
        """
        import datetime as _dt
        # Primaer: Alpaca Market Data API (Echtzeit-Kurse)
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            data_client = StockHistoricalDataClient(
                api_key=self._api_key,
                secret_key=self._api_secret,
            )
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quotes = data_client.get_stock_latest_quote(req)
            if ticker in quotes:
                q = quotes[ticker]
                bid = float(q.bid_price) if q.bid_price else 0.0
                ask = float(q.ask_price) if q.ask_price else 0.0
                last = (bid + ask) / 2.0 if bid and ask else bid or ask
                return Quote(
                    ticker=ticker,
                    bid=bid,
                    ask=ask,
                    last=last,
                    timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                )
        except Exception:
            pass  # Fallback auf yfinance-Cache

        # Fallback: yfinance-Cache (mit Freshness-Check)
        from ..common.data_loader import get_prices
        prices = get_prices(ticker, period="5d")
        last = float(prices["close"].iloc[-1])
        return Quote(
            ticker=ticker,
            bid=last * 0.999,
            ask=last * 1.001,
            last=last,
            timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        )

    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def place_order(
        self,
        ticker:     str,
        side:       str,
        qty:        float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        client_id:  Optional[str] = None,
    ) -> OrderResult:
        client = self._ensure_client()
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        if order_type == "market":
            req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side_enum,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_id,
            )
        elif order_type == "limit":
            if limit_price is None:
                return OrderResult(
                    order_id="", status="rejected",
                    ticker=ticker, side=side, qty=qty,
                    error="limit order without limit_price",
                )
            req = LimitOrderRequest(
                symbol=ticker, qty=qty, side=side_enum,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_id,
            )
        else:
            return OrderResult(
                order_id="", status="rejected",
                ticker=ticker, side=side, qty=qty,
                error=f"unsupported order_type: {order_type}",
            )
        order = client.submit_order(req)
        return _from_alpaca_order(order)

    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def get_order(self, order_id: str) -> OrderResult:
        client = self._ensure_client()
        order = client.get_order_by_id(order_id)
        return _from_alpaca_order(order)

    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def cancel_order(self, order_id: str) -> bool:
        client = self._ensure_client()
        try:
            client.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    @api_retry(attempts=3, min_wait=2, max_wait=10)
    def list_orders(self, status: str = "open") -> list[OrderResult]:
        client = self._ensure_client()
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        status_map = {
            "open":  QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all":   QueryOrderStatus.ALL,
        }
        req = GetOrdersRequest(status=status_map.get(status, QueryOrderStatus.OPEN))
        orders = client.get_orders(req)
        return [_from_alpaca_order(o) for o in orders]


def _from_alpaca_order(order) -> OrderResult:
    """Konvertiert alpaca-py Order -> OrderResult."""
    side_str = "buy" if str(order.side).lower().endswith("buy") else "sell"
    status_str = str(order.status).lower().split(".")[-1]  # 'orderstatus.filled' -> 'filled'
    avg_fill = float(order.filled_avg_price) if getattr(order, "filled_avg_price", None) else None
    filled_qty = float(order.filled_qty) if getattr(order, "filled_qty", None) else 0.0
    return OrderResult(
        order_id=str(order.id),
        status=status_str,
        ticker=order.symbol,
        side=side_str,
        qty=float(order.qty) if order.qty else 0.0,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill,
        submitted_at=str(order.submitted_at) if order.submitted_at else None,
        filled_at=str(order.filled_at) if order.filled_at else None,
        raw={"alpaca_status": str(order.status)},
    )

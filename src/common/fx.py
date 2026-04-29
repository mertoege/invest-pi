"""
FX-Layer — EUR/USD Wechselkurs.

Holt EUR/USD (= EURUSD=X auf yfinance) einmal pro 24h, cached in market.db
(reuse fundamentals-Tabelle mit ticker='_FX_EURUSD'). Bei Fail: fallback auf
0.92 (April 2026 Stand).

Usage:
    from src.common.fx import eur_per_usd
    rate = eur_per_usd()    # 0.91 ish
    eur_value = usd_value * rate
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from .retry import yfinance_retry
from .storage import MARKET_DB, connect

log = logging.getLogger("invest_pi.fx")

DEFAULT_RATE = 0.92
CACHE_HOURS = 24
FX_TICKER = "_FX_EURUSD"


def _load_cached() -> Optional[float]:
    try:
        with connect(MARKET_DB) as conn:
            row = conn.execute(
                "SELECT pe_ratio AS rate, updated_at FROM fundamentals WHERE ticker = ?",
                (FX_TICKER,),
            ).fetchone()
        if not row:
            return None
        ts = dt.datetime.fromisoformat(row["updated_at"])
        if (dt.datetime.now() - ts).total_seconds() < CACHE_HOURS * 3600:
            return float(row["rate"])
    except Exception as e:
        log.debug(f"fx cache read failed: {e}")
    return None


def _save_cached(rate: float) -> None:
    try:
        with connect(MARKET_DB) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO fundamentals
                   (ticker, name, pe_ratio, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (FX_TICKER, "EUR/USD", rate, dt.datetime.now().isoformat()),
            )
    except Exception as e:
        log.warning(f"fx cache write failed: {e}")


@yfinance_retry
def _fetch_live() -> Optional[float]:
    try:
        import yfinance as yf
        ticker = yf.Ticker("EURUSD=X")
        # EURUSD=X gibt USD pro EUR (1 EUR = X USD), wir wollen EUR pro USD
        # Aber eigentlich: yfinance gibt fuer EURUSD=X den Wert "wieviele USD pro 1 EUR".
        # Wir wollen EUR pro USD = 1 / dieser Wert.
        info = ticker.history(period="2d", interval="1d")
        if info.empty:
            return None
        usd_per_eur = float(info["Close"].iloc[-1])
        if usd_per_eur <= 0:
            return None
        return 1.0 / usd_per_eur
    except Exception as e:
        log.warning(f"fx live fetch failed: {e}")
        return None


def eur_per_usd(force_refresh: bool = False) -> float:
    """Live-Wechselkurs mit 24h-Cache. Fallback auf DEFAULT_RATE."""
    if not force_refresh:
        cached = _load_cached()
        if cached is not None:
            return cached
    live = _fetch_live()
    if live is not None:
        _save_cached(live)
        return live
    # Last-resort: was im Cache liegt (auch wenn alt) oder Fallback
    cached_old = _load_cached()
    if cached_old is not None:
        return cached_old
    return DEFAULT_RATE


if __name__ == "__main__":
    print(f"Current EUR/USD rate: {eur_per_usd():.4f} EUR per 1 USD")

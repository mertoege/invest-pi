"""
Zentraler Daten-Lader: yfinance mit SQLite-Caching.

Lädt Kursdaten einmal und speichert sie lokal in market.db. Wiederholte Aufrufe
greifen auf Cache zu — spart Traffic und macht Offline-Arbeit möglich.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd
import yfinance as yf

from .storage import MARKET_DB, connect


# ────────────────────────────────────────────────────────────
# PRICES
# ────────────────────────────────────────────────────────────
def get_prices(
    ticker: str,
    period: str = "10y",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Lade historische OHLCV-Daten für einen Ticker.

    Args:
        ticker: Yahoo-Finance-Symbol, z.B. "NVDA" oder "ASML.AS"
        period: yfinance-Format: "1mo", "1y", "5y", "10y", "max"
        force_refresh: Wenn True, wird Cache ignoriert und neu geladen

    Returns:
        DataFrame indexed by date, Spalten: open, high, low, close, volume
    """
    if not force_refresh:
        cached = _load_from_cache(ticker)
        if cached is not None and len(cached) > 100:
            return cached

    print(f"  ↓ lade {ticker} von Yahoo Finance (period={period})…")
    raw = yf.Ticker(ticker).history(period=period, auto_adjust=True)

    if raw.empty:
        raise ValueError(f"Keine Daten für {ticker} erhalten")

    # Normalisieren
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"

    _save_to_cache(ticker, df)
    return df


def _load_from_cache(ticker: str) -> Optional[pd.DataFrame]:
    with connect(MARKET_DB) as conn:
        cur = conn.execute(
            "SELECT date, open, high, low, close, volume "
            "FROM prices WHERE ticker = ? ORDER BY date",
            (ticker,),
        )
        rows = cur.fetchall()

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    return df


def _save_to_cache(ticker: str, df: pd.DataFrame) -> None:
    rows = [
        (
            ticker,
            idx.strftime("%Y-%m-%d"),
            float(row["open"])   if pd.notna(row["open"])   else None,
            float(row["high"])   if pd.notna(row["high"])   else None,
            float(row["low"])    if pd.notna(row["low"])    else None,
            float(row["close"])  if pd.notna(row["close"])  else None,
            int(row["volume"])   if pd.notna(row["volume"]) else None,
        )
        for idx, row in df.iterrows()
    ]
    with connect(MARKET_DB) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO prices "
            "(ticker, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# ────────────────────────────────────────────────────────────
# FUNDAMENTALS
# ────────────────────────────────────────────────────────────
def get_fundamentals(ticker: str, force_refresh: bool = False) -> dict:
    """Lädt fundamentale Kennzahlen (P/E, Market Cap, etc.) mit Caching."""
    if not force_refresh:
        with connect(MARKET_DB) as conn:
            cur = conn.execute(
                "SELECT * FROM fundamentals WHERE ticker = ?", (ticker,)
            )
            row = cur.fetchone()
        if row and row["updated_at"]:
            updated = dt.datetime.fromisoformat(row["updated_at"])
            # Cache gültig für 24 h
            if (dt.datetime.now() - updated).total_seconds() < 86400:
                return dict(row)

    info = yf.Ticker(ticker).info
    data = {
        "ticker":       ticker,
        "name":         info.get("longName") or ticker,
        "sector":       info.get("sector"),
        "market_cap":   info.get("marketCap"),
        "pe_ratio":     info.get("trailingPE"),
        "pb_ratio":     info.get("priceToBook"),
        "dividend_yld": info.get("dividendYield"),
        "beta":         info.get("beta"),
        "updated_at":   dt.datetime.now().isoformat(),
    }
    with connect(MARKET_DB) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO fundamentals
                (ticker, name, sector, market_cap, pe_ratio, pb_ratio,
                 dividend_yld, beta, updated_at)
            VALUES (:ticker, :name, :sector, :market_cap, :pe_ratio,
                    :pb_ratio, :dividend_yld, :beta, :updated_at)
            """,
            data,
        )
    return data

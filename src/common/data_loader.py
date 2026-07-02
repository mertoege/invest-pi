"""
Zentraler Daten-Lader: yfinance mit SQLite-Caching.

Lädt Kursdaten und speichert sie lokal in market.db. Cache wird automatisch
aufgefrischt wenn die neuesten Daten aelter als 1 Handelstag sind.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Optional

import pandas as pd
import yfinance as yf

from .storage import MARKET_DB, connect

log = logging.getLogger("invest_pi.data_loader")

# Minimaler Abstand zwischen yfinance-Requests (Rate-Limit-Schutz)
_RATE_LIMIT_SECONDS = 0.3
_last_request_ts: float = 0.0


def _is_cache_stale(df: pd.DataFrame, max_age_hours: int = 18) -> bool:
    """
    Prueft ob der Cache aufgefrischt werden muss.

    Logik: wenn die letzte gecachte Zeile aelter als max_age_hours ist
    UND mindestens ein Handelstag vergangen ist (Mo-Fr), ist der Cache stale.
    Default 18h = wenn der Score-Job um 12:30 CEST laeuft und die letzten
    Daten von gestern 00:00 sind (~36h alt), wird refreshed.
    """
    if df.empty:
        return True
    last_date = df.index[-1]
    # Normalisiere auf date (ohne time)
    if hasattr(last_date, "date"):
        last_date = last_date.date() if callable(last_date.date) else last_date
    else:
        last_date = pd.Timestamp(last_date).date()

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    # Wie viele Handelstage (Mo-Fr) liegen zwischen last_date und heute?
    trading_days_missed = 0
    d = last_date + dt.timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:  # Mo=0 .. Fr=4
            trading_days_missed += 1
        d += dt.timedelta(days=1)

    # Stale wenn mindestens 1 Handelstag fehlt
    return trading_days_missed >= 1


def _rate_limited_fetch(ticker: str, period: str) -> pd.DataFrame:
    """yfinance-Request mit Rate-Limiting."""
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)

    raw = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    _last_request_ts = time.monotonic()
    return raw


# ────────────────────────────────────────────────────────────
# PRICES
# ────────────────────────────────────────────────────────────

_PERIOD_TO_DAYS = {
    "1d": 1, "5d": 5, "1wk": 7, "1mo": 30, "3mo": 90,
    "6mo": 180, "1y": 365, "2y": 730, "5y": 1825, "10y": 3650, "max": 99999,
}


def _trim_to_period(df: "pd.DataFrame", period: str) -> "pd.DataFrame":
    days = _PERIOD_TO_DAYS.get(period, 99999)
    if days >= 99999 or df.empty:
        return df
    cutoff = df.index[-1] - pd.Timedelta(days=int(days * 1.5))
    trimmed = df[df.index >= cutoff]
    return trimmed if len(trimmed) >= 2 else df

def get_prices(
    ticker: str,
    period: str = "10y",
    force_refresh: bool = False,
    max_cache_age_hours: int = 18,
) -> pd.DataFrame:
    """
    Lade historische OHLCV-Daten für einen Ticker.

    Args:
        ticker: Yahoo-Finance-Symbol, z.B. "NVDA" oder "ASML.AS"
        period: yfinance-Format: "1mo", "1y", "5y", "10y", "max"
        force_refresh: Wenn True, wird Cache ignoriert und neu geladen
        max_cache_age_hours: Cache wird aufgefrischt wenn aelter (default 18h)

    Returns:
        DataFrame indexed by date, Spalten: open, high, low, close, volume
    """
    if not force_refresh:
        cached = _load_from_cache(ticker)
        if cached is not None and len(cached) > 100:
            if not _is_cache_stale(cached, max_cache_age_hours):
                return _trim_to_period(cached, period)
            # Cache ist stale → inkrementelles Update versuchen
            try:
                fresh = _incremental_update(ticker, cached)
                if fresh is not None:
                    return _trim_to_period(fresh, period)
            except Exception as e:
                log.warning(f"incremental update failed for {ticker}: {e}")
                # Fallback: vollstaendigen Refresh versuchen

    try:
        print(f"  ↓ lade {ticker} von Yahoo Finance (period={period})…")
        raw = _rate_limited_fetch(ticker, period)
    except Exception as e:
        # Bei Netzwerk-Fehler: stale Cache ist besser als kein Cache
        cached = _load_from_cache(ticker)
        if cached is not None and len(cached) > 0:
            log.warning(f"yfinance failed for {ticker}, using stale cache: {e}")
            return _trim_to_period(cached, period)
        raise ValueError(f"Keine Daten für {ticker} erhalten: {e}")

    if raw.empty:
        cached = _load_from_cache(ticker)
        if cached is not None and len(cached) > 0:
            log.warning(f"yfinance returned empty for {ticker}, using stale cache")
            return _trim_to_period(cached, period)
        raise ValueError(f"Keine Daten für {ticker} erhalten")

    # Normalisieren
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"

    _save_to_cache(ticker, df)
    return _trim_to_period(df, period)


def _incremental_update(ticker: str, cached: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Holt nur die letzten 5 Tage und merged sie in den bestehenden Cache.
    Viel schneller als full-refresh bei 10y-Daten.
    """
    raw = _rate_limited_fetch(ticker, "5d")
    if raw.empty:
        return None

    fresh = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
    fresh.index = pd.to_datetime(fresh.index).tz_localize(None).normalize()
    fresh.index.name = "date"

    # Audit-Fix (Fable5 2026-07-02): Adjustierungs-Bruch erkennen. yfinance liefert
    # rueckwirkend split-/dividenden-adjustierte Kurse; der alte Cache bleibt auf alter
    # Basis. Weicht ein Overlap-Tag (gleicher Handelstag in Cache UND frischem Fetch) um
    # >0.5% ab, wurde neu adjustiert -> mergen erzeugt einen Fake-Sprung (Split -> der
    # MAX_DAY_JUMP-Filter wirft den Ticker ~18 Monate aus dem Momentum-Ranking; Dividende
    # -> verzerrtes 6M-Momentum). Dann None -> Aufrufer laedt die Historie voll neu.
    overlap = cached.index.intersection(fresh.index)
    if len(overlap):
        c_last, f_last = cached.loc[overlap[-1], "close"], fresh.loc[overlap[-1], "close"]
        if c_last and float(c_last) > 0 and abs(float(f_last) / float(c_last) - 1) > 0.005:
            log.warning(f"{ticker}: Adjustierungs-Bruch ({float(c_last):.2f}->{float(f_last):.2f}) - full refresh")
            return None

    # Merge: neue Tage anhaengen, bestehende ueberschreiben
    combined = pd.concat([cached, fresh])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()

    _save_to_cache(ticker, combined)
    return combined


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
    dates = df.index.strftime("%Y-%m-%d")
    tickers = [ticker] * len(df)
    opens  = [float(v) if pd.notna(v) else None for v in df["open"].values]
    highs  = [float(v) if pd.notna(v) else None for v in df["high"].values]
    lows   = [float(v) if pd.notna(v) else None for v in df["low"].values]
    closes = [float(v) if pd.notna(v) else None for v in df["close"].values]
    vols   = [int(v)   if pd.notna(v) else None for v in df["volume"].values]
    rows = list(zip(tickers, dates, opens, highs, lows, closes, vols))
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

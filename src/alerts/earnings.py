"""
Earnings Calendar Awareness — 10. Risk-Dimension.

Erhoehtes Risiko rund um Earnings-Termine:
  - 3 Tage vor Earnings: Gap-Risk, IV-Crush-Risk
  - Earnings-Day: maximale Unsicherheit
  - Post-Earnings: PEAD (Post-Earnings Announcement Drift)

Datenquelle: yfinance Ticker.calendar (kostenlos, kein API-Key).

Scoring:
  - 0-3 Tage vor Earnings: Score 40-80 (gestaffelt)
  - Earnings heute: Score 80
  - 1-5 Tage nach Earnings: Score 20-40 (PEAD-Awareness)
  - Sonst: Score 0
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

log = logging.getLogger("invest_pi.earnings")


def get_next_earnings_date(ticker: str) -> Optional[dt.date]:
    """
    Holt naechsten Earnings-Termin via yfinance.
    Returns None wenn kein Termin bekannt.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None
        # yfinance calendar format varies — handle both dict and DataFrame
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                if isinstance(ed, list) and len(ed) > 0:
                    return _to_date(ed[0])
                return _to_date(ed)
        else:
            # DataFrame format
            if "Earnings Date" in cal.index:
                vals = cal.loc["Earnings Date"]
                if hasattr(vals, "iloc") and len(vals) > 0:
                    return _to_date(vals.iloc[0])
                return _to_date(vals)
        return None
    except Exception as e:
        log.debug(f"earnings date fetch for {ticker}: {e}")
        return None


def get_last_earnings_date(ticker: str) -> Optional[dt.date]:
    """
    Holt letzten Earnings-Termin aus der Earnings-History.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # earnings_dates gibt historische + zukuenftige Termine
        dates = t.earnings_dates
        if dates is None or dates.empty:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        past = dates[dates.index <= now]
        if past.empty:
            return None
        return past.index[-1].date()
    except Exception as e:
        log.debug(f"earnings history for {ticker}: {e}")
        return None


def _to_date(val) -> Optional[dt.date]:
    """Konvertiert verschiedene Formate zu date."""
    if val is None:
        return None
    if isinstance(val, dt.date):
        return val
    if isinstance(val, dt.datetime):
        return val.date()
    if hasattr(val, "date"):
        return val.date()
    try:
        import pandas as pd
        return pd.Timestamp(val).date()
    except Exception:
        return None


def compute_earnings_risk(ticker: str) -> dict:
    """
    Berechnet Earnings-Risk-Score.

    Returns:
        {
            "available": True/False,
            "next_earnings": "2026-05-15" oder None,
            "days_until": int oder None,
            "days_since_last": int oder None,
            "score": float (0-100),
            "triggered": bool,
            "reason": str,
        }
    """
    today = dt.date.today()

    next_date = get_next_earnings_date(ticker)
    last_date = get_last_earnings_date(ticker)

    days_until = None
    days_since = None
    score = 0.0
    reasons = []

    if next_date:
        days_until = (next_date - today).days

        # Pre-Earnings Risk Zone: 0-7 Tage vorher
        if days_until == 0:
            score = 80.0
            reasons.append("Earnings HEUTE")
        elif 1 <= days_until <= 3:
            score = 60.0 + (3 - days_until) * 10  # 60-80
            reasons.append(f"Earnings in {days_until}d — Gap-Risk")
        elif 4 <= days_until <= 7:
            score = 30.0 + (7 - days_until) * 8  # 30-54
            reasons.append(f"Earnings in {days_until}d — erhoehte Vola")

    if last_date:
        days_since = (today - last_date).days

        # Post-Earnings: PEAD-Zone (1-5 Tage danach)
        if 0 <= days_since <= 5:
            pead_score = max(0, 40 - days_since * 8)  # 40 → 0
            if pead_score > score:
                score = pead_score
                reasons.append(f"Post-Earnings Drift ({days_since}d seit Earnings)")

    score = min(100.0, score)
    triggered = score >= 35

    return {
        "available": next_date is not None or last_date is not None,
        "next_earnings": str(next_date) if next_date else None,
        "days_until": days_until,
        "days_since_last": days_since,
        "score": round(score, 1),
        "triggered": triggered,
        "reason": "; ".join(reasons) if reasons else "kein Earnings-Termin in Sicht",
    }

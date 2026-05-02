"""
Market Breadth Internals — Advance/Decline, New Highs/Lows, McClellan.

Misst die "Gesundheit" des breiten Marktes jenseits von Index-Levels.
Ein steigender S&P500 bei verschlechternder Breadth ist ein klassisches
Warnsignal (Distribution-Phase).

Datenquellen (alle kostenlos via yfinance):
  - ^ADDN / ^ADDQ: NYSE/NASDAQ Advance-Decline (wenn verfuegbar)
  - Fallback: Top-50 S&P-Bestandteile als Proxy berechnen
  - SPY vs RSP (equal-weight): Divergenz = wenige Mega-Caps treiben Index
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

import numpy as np


# ────────────────────────────────────────────────────────────
# CACHE (6h TTL wie FRED)
# ────────────────────────────────────────────────────────────
_CACHE_DIR = None
CACHE_TTL_SECONDS = 6 * 3600


def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        from ..common.storage import DATA_DIR
        _CACHE_DIR = DATA_DIR / "breadth_cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _read_cache(key: str) -> Optional[dict]:
    p = _get_cache_dir() / f"{key}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        fetched = dt.datetime.fromisoformat(data["_fetched_at"])
        age = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - fetched).total_seconds()
        if age > CACHE_TTL_SECONDS:
            return None
        return data
    except Exception:
        return None


def _write_cache(key: str, data: dict) -> None:
    data["_fetched_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    try:
        (_get_cache_dir() / f"{key}.json").write_text(json.dumps(data))
    except Exception:
        pass


# ────────────────────────────────────────────────────────────
# BREADTH PROXY: SPY vs RSP Divergenz
# ────────────────────────────────────────────────────────────
def spy_rsp_divergence(lookback_days: int = 30) -> dict:
    """
    Vergleicht SPY (cap-weighted) mit RSP (equal-weight S&P500).
    Wenn SPY steigt aber RSP zurueckbleibt, treiben nur wenige Mega-Caps
    den Index → schlechte Breadth.

    Returns:
        {
          "spy_return": float,
          "rsp_return": float,
          "divergence": float,  # spy - rsp (positiv = schlechte breadth)
          "signal":     str,    # "healthy" | "diverging" | "narrow_leadership"
          "score":      float,  # 0-100
        }
    """
    cached = _read_cache("spy_rsp")
    if cached:
        return cached

    try:
        from ..common.data_loader import get_prices
        spy = get_prices("SPY", period="3mo")["close"]
        rsp = get_prices("RSP", period="3mo")["close"]

        if len(spy) < lookback_days or len(rsp) < lookback_days:
            return {"error": "insufficient data", "score": 0}

        spy_ret = float(spy.iloc[-1] / spy.iloc[-lookback_days] - 1)
        rsp_ret = float(rsp.iloc[-1] / rsp.iloc[-lookback_days] - 1)
        divergence = spy_ret - rsp_ret

        if divergence > 0.05:
            signal = "narrow_leadership"
            score = min(80.0, divergence * 500)
        elif divergence > 0.02:
            signal = "diverging"
            score = min(50.0, divergence * 300)
        else:
            signal = "healthy"
            score = 0.0

        result = {
            "spy_return": round(spy_ret, 4),
            "rsp_return": round(rsp_ret, 4),
            "divergence": round(divergence, 4),
            "signal": signal,
            "score": round(score, 1),
        }
        _write_cache("spy_rsp", result)
        return result
    except Exception as e:
        return {"error": str(e), "score": 0}


# ────────────────────────────────────────────────────────────
# BREADTH PROXY: Percent Above MA200
# ────────────────────────────────────────────────────────────
# Repraesentatives Sample aus verschiedenen Sektoren
BREADTH_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "V", "JNJ", "UNH", "PG", "XOM", "HD", "MA",
    "CVX", "ABBV", "MRK", "PEP", "COST", "KO", "LLY", "AVGO",
    "WMT", "TMO", "CRM", "MCD", "ACN", "CSCO", "ABT", "DHR",
    "NEE", "TXN", "PM", "RTX", "BMY", "UPS", "SCHW", "INTC",
]


def pct_above_ma200(universe: list[str] | None = None) -> dict:
    """
    Berechnet wieviel Prozent eines Ticker-Universums ueber ihrer MA200 liegen.
    < 40% = schwache Breadth, > 70% = gesunde Breadth.

    Returns:
        {
          "pct_above": float (0-1),
          "n_total": int,
          "n_above": int,
          "score":   float (0-100),
        }
    """
    cached = _read_cache("pct_above_ma200")
    if cached:
        return cached

    tickers = universe or BREADTH_UNIVERSE
    n_above = 0
    n_total = 0

    from ..common.data_loader import get_prices

    for ticker in tickers:
        try:
            prices = get_prices(ticker, period="1y")["close"]
            if len(prices) < 200:
                continue
            current = float(prices.iloc[-1])
            ma200 = float(prices.tail(200).mean())
            n_total += 1
            if current > ma200:
                n_above += 1
        except Exception:
            continue

    if n_total == 0:
        return {"error": "no data", "score": 0}

    pct = n_above / n_total

    # Score: niedrige Breadth = hohes Risiko
    if pct < 0.30:
        score = 60.0
    elif pct < 0.40:
        score = 40.0
    elif pct < 0.50:
        score = 20.0
    else:
        score = 0.0

    result = {
        "pct_above": round(pct, 3),
        "n_total": n_total,
        "n_above": n_above,
        "score": score,
    }
    _write_cache("pct_above_ma200", result)
    return result


# ────────────────────────────────────────────────────────────
# COMBINED BREADTH SCORE
# ────────────────────────────────────────────────────────────
def market_breadth_score() -> dict:
    """
    Aggregierter Market-Breadth-Score.
    Kombiniert SPY/RSP-Divergenz (50%) + Pct-Above-MA200 (50%).

    Returns:
        {
          "score":      0-100,
          "triggered":  bool,
          "reasons":    [str],
          "spy_rsp":    dict,
          "pct_ma200":  dict,
        }
    """
    spy_rsp = spy_rsp_divergence()
    ma200 = pct_above_ma200()

    sub1 = spy_rsp.get("score", 0)
    sub2 = ma200.get("score", 0)
    score = (sub1 * 0.5) + (sub2 * 0.5)
    score = min(100.0, score)

    reasons = []
    if spy_rsp.get("signal") == "narrow_leadership":
        reasons.append(f"Narrow Leadership: SPY {spy_rsp['spy_return']:+.1%} vs RSP {spy_rsp['rsp_return']:+.1%}")
    elif spy_rsp.get("signal") == "diverging":
        reasons.append(f"SPY/RSP diverging: {spy_rsp['divergence']:+.1%}")

    pct = ma200.get("pct_above")
    if pct is not None and pct < 0.50:
        reasons.append(f"Nur {pct:.0%} der Aktien ueber MA200")

    return {
        "score": round(score, 1),
        "triggered": score >= 30,
        "reasons": reasons,
        "spy_rsp": spy_rsp,
        "pct_ma200": ma200,
    }

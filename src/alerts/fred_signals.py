"""
FRED Cross-Asset Macro Signals — Yield Curve, Credit Spreads, Dollar Index.

Nutzt die FRED API (Federal Reserve Economic Data) fuer robuste Makro-Signale
die ueber das hinausgehen, was yfinance alleine liefert.

FRED ist kostenlos. Mit API-Key: 120 req/min. Ohne Key: 30 req/min ueber
die JSON-URL (kein fredapi-Package noetig).

Serien:
  - T10Y2Y:  10-Year minus 2-Year Treasury Spread (Yield Curve)
  - BAMLC0A4CBBB: BofA BBB Corporate Bond Spread (Credit Stress)
  - DTWEXBGS: Trade-Weighted Dollar Index (Broad, Goods & Services)
  - T10YIE:  10-Year Breakeven Inflation Rate
  - VIXCLS:  CBOE VIX (als Backup/Cross-Check)

Cache: 6h TTL — Makro-Daten aendern sich nur einmal taeglich.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Cache in data-dir
_CACHE_DIR = None

def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        from ..common.storage import DATA_DIR
        _CACHE_DIR = DATA_DIR / "fred_cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
CACHE_TTL_SECONDS = 6 * 3600  # 6 Stunden


@dataclass
class FREDSeries:
    series_id: str
    value: Optional[float]
    date: Optional[str]
    fetched_at: str
    error: Optional[str] = None


# ────────────────────────────────────────────────────────────
# FETCH (mit Cache)
# ────────────────────────────────────────────────────────────
def _cache_path(series_id: str) -> Path:
    return _get_cache_dir() / f"{series_id}.json"


def _read_cache(series_id: str) -> Optional[FREDSeries]:
    p = _cache_path(series_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        fetched = dt.datetime.fromisoformat(data["fetched_at"])
        age = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - fetched).total_seconds()
        if age > CACHE_TTL_SECONDS:
            return None
        return FREDSeries(**data)
    except Exception:
        return None


def _write_cache(series: FREDSeries) -> None:
    try:
        _cache_path(series.series_id).write_text(
            json.dumps({
                "series_id": series.series_id,
                "value": series.value,
                "date": series.date,
                "fetched_at": series.fetched_at,
                "error": series.error,
            })
        )
    except Exception:
        pass


def fetch_fred_series(series_id: str, lookback_days: int = 90) -> FREDSeries:
    """
    Holt die letzte Observation einer FRED-Serie.
    Versucht zuerst fredapi (falls installiert + Key da), dann JSON-URL-Fallback.
    """
    cached = _read_cache(series_id)
    if cached is not None:
        return cached

    now_str = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    result = None

    # Versuch 1: fredapi package
    if FRED_API_KEY:
        try:
            from fredapi import Fred
            fred = Fred(api_key=FRED_API_KEY)
            data = fred.get_series(
                series_id,
                observation_start=dt.date.today() - dt.timedelta(days=lookback_days),
            )
            data = data.dropna()
            if len(data) > 0:
                result = FREDSeries(
                    series_id=series_id,
                    value=float(data.iloc[-1]),
                    date=str(data.index[-1].date()),
                    fetched_at=now_str,
                )
        except Exception:
            pass

    # Versuch 2: Direct JSON URL (kein Key noetig, 30 req/min)
    if result is None:
        try:
            import urllib.request
            end = dt.date.today()
            start = end - dt.timedelta(days=lookback_days)
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={series_id}"
                f"&observation_start={start}"
                f"&observation_end={end}"
                f"&sort_order=desc"
                f"&limit=5"
                f"&file_type=json"
            )
            if FRED_API_KEY:
                url += f"&api_key={FRED_API_KEY}"
            else:
                # Ohne Key geht nur mit dem Demo-Key
                url += "&api_key=DEMO_KEY"

            req = urllib.request.Request(url, headers={"User-Agent": "InvestPi/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            observations = data.get("observations", [])
            # Finde erste non-"." Observation (FRED nutzt "." fuer missing)
            for obs in observations:
                if obs["value"] not in (".", ""):
                    result = FREDSeries(
                        series_id=series_id,
                        value=float(obs["value"]),
                        date=obs["date"],
                        fetched_at=now_str,
                    )
                    break
        except Exception as e:
            result = FREDSeries(
                series_id=series_id,
                value=None,
                date=None,
                fetched_at=now_str,
                error=str(e),
            )

    if result is None:
        result = FREDSeries(
            series_id=series_id,
            value=None,
            date=None,
            fetched_at=now_str,
            error="no data from fredapi or JSON fallback",
        )

    _write_cache(result)
    return result


# ────────────────────────────────────────────────────────────
# CONVENIENCE: Alle Makro-Signale auf einmal
# ────────────────────────────────────────────────────────────
MACRO_SERIES = {
    "yield_curve":      "T10Y2Y",       # 10Y - 2Y spread
    "credit_spread":    "BAMLC0A4CBBB", # BBB corporate spread
    "dollar_index":     "DTWEXBGS",     # Trade-weighted USD
    "breakeven_infl":   "T10YIE",       # 10Y breakeven inflation
    "vix":              "VIXCLS",       # VIX daily close
}


def fetch_all_macro() -> dict[str, FREDSeries]:
    """Holt alle 5 Makro-Serien (mit Cache). Returns {name: FREDSeries}."""
    return {name: fetch_fred_series(sid) for name, sid in MACRO_SERIES.items()}


def macro_risk_score() -> dict:
    """
    Berechnet einen aggregierten Makro-Risk-Score aus FRED-Daten.

    Returns:
        {
          "score":     0-100,
          "triggered": bool,
          "reasons":   [str],
          "details":   {name: {value, date, contribution}},
        }
    """
    signals = fetch_all_macro()
    score = 0.0
    reasons = []
    details = {}

    # 1. Yield Curve (T10Y2Y)
    yc = signals.get("yield_curve")
    if yc and yc.value is not None:
        spread = yc.value
        details["yield_curve"] = {"value": spread, "date": yc.date}
        if spread < -0.5:
            contrib = 25
            reasons.append(f"Yield-Curve tief invertiert ({spread:+.2f}%)")
        elif spread < 0:
            contrib = 15
            reasons.append(f"Yield-Curve invertiert ({spread:+.2f}%)")
        elif spread < 0.3:
            contrib = 8
            reasons.append(f"Yield-Curve flach ({spread:+.2f}%)")
        else:
            contrib = 0
        score += contrib
        details["yield_curve"]["contribution"] = contrib

    # 2. Credit Spread (BBB)
    cs = signals.get("credit_spread")
    if cs and cs.value is not None:
        spread = cs.value
        details["credit_spread"] = {"value": spread, "date": cs.date}
        if spread > 3.0:
            contrib = 25
            reasons.append(f"Credit-Spread hoch ({spread:.2f}%, Stress)")
        elif spread > 2.0:
            contrib = 15
            reasons.append(f"Credit-Spread erhoht ({spread:.2f}%)")
        elif spread > 1.5:
            contrib = 5
            reasons.append(f"Credit-Spread leicht erhoht ({spread:.2f}%)")
        else:
            contrib = 0
        score += contrib
        details["credit_spread"]["contribution"] = contrib

    # 3. Dollar Index (starker Dollar = Headwind fuer US-Equities + EM)
    dx = signals.get("dollar_index")
    if dx and dx.value is not None:
        val = dx.value
        details["dollar_index"] = {"value": val, "date": dx.date}
        # Dollar ueber 130 = starker Headwind
        if val > 135:
            contrib = 15
            reasons.append(f"Dollar sehr stark ({val:.1f})")
        elif val > 128:
            contrib = 8
            reasons.append(f"Dollar stark ({val:.1f})")
        else:
            contrib = 0
        score += contrib
        details["dollar_index"]["contribution"] = contrib

    # 4. Breakeven Inflation (steigende Inflation = hawkish Fed)
    bi = signals.get("breakeven_infl")
    if bi and bi.value is not None:
        val = bi.value
        details["breakeven_infl"] = {"value": val, "date": bi.date}
        if val > 3.0:
            contrib = 15
            reasons.append(f"Breakeven-Inflation hoch ({val:.2f}%)")
        elif val > 2.5:
            contrib = 8
            reasons.append(f"Breakeven-Inflation erhoht ({val:.2f}%)")
        elif val < 1.5:
            contrib = 10
            reasons.append(f"Breakeven-Inflation niedrig ({val:.2f}%, Deflationsrisiko)")
        else:
            contrib = 0
        score += contrib
        details["breakeven_infl"]["contribution"] = contrib

    # 5. VIX (Cross-Check mit yfinance-Wert)
    vix = signals.get("vix")
    if vix and vix.value is not None:
        val = vix.value
        details["vix_fred"] = {"value": val, "date": vix.date}
        if val > 30:
            contrib = 20
            reasons.append(f"VIX(FRED) {val:.1f} (Stress)")
        elif val > 22:
            contrib = 10
            reasons.append(f"VIX(FRED) {val:.1f} (elevated)")
        else:
            contrib = 0
        score += contrib
        details["vix_fred"]["contribution"] = contrib

    score = min(100.0, score)
    triggered = score >= 35

    return {
        "score": score,
        "triggered": triggered,
        "reasons": reasons,
        "details": details,
    }

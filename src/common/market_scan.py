#!/usr/bin/env python3
"""
market_scan.py — Breiter Markt-Scan als Analyse-Grundlage fuer den Monats-Sparplan.

WARUM ES DAS GIBT (Audit 2026-08-01):
Der Monats-Sparplan (scripts/monthly_dca.py) zog seine Kandidaten aus den
`daily_score`-Risk-Scores der letzten 24h. Diese Scores erzeugte faktisch nur noch
der DCA-Watchdog (18:00) — und der bewertet ausschliesslich die BEREITS gehaltenen
Positionen. Ergebnis: die Kandidatenliste bestand dauerhaft aus genau 4 Tickern
(NVDA, ASML, PG, SPY), die Kaufregel "mind. 2 Kandidaten mit composite<30 und
alert_level 0" war praktisch nie erfuellbar, und der Sparplan fiel Monat fuer Monat
auf den ETF-Fallback (VWCE.DE) zurueck. Das sah nach einer Entscheidung aus, war
aber ein Zwangsergebnis.

Dieses Modul liefert stattdessen eine ECHTE Auswahlgrundlage: harte, lokal berechnete
Kennzahlen ueber das breite Momentum-Universum (src/common/universe.py) plus die drei
auf Revolut handelbaren UCITS-ETFs. Keine LLM-Kosten, keine Vorhersage — nur Fakten,
ueber die das LLM in monthly_dca.py danach urteilen kann.

Kennzahlen je Ticker:
  mom_1m / mom_3m / mom_6m / mom_12m  — Rendite ueber die Periode
  mom_12_1                            — 12M-Momentum OHNE den letzten Monat
                                        (akademischer Standard; der letzte Monat
                                         zeigt empirisch Umkehr statt Fortsetzung)
  vs_200d                             — Abstand zum 200-Tage-Schnitt (Trendfilter)
  dd_from_52w_high                    — Ruecksetzer vom 52-Wochen-Hoch
  vol_annual                          — annualisierte Schwankung (Tagesrenditen)
  trend_ok                            — Kurs ueber 200-Tage-Schnitt

Sanity-Guards analog scripts/momentum_rebalance.py, damit Split-/Daten-Glitches
nicht als Momentum-Wunder durchrutschen.
"""

from __future__ import annotations

import logging

import numpy as np

from .data_loader import get_prices
from .universe import UNIVERSE

log = logging.getLogger("invest_pi.market_scan")

# Auf Merts Broker (Revolut) in EUR handelbare UCITS-ETFs — dieselbe Liste,
# die monthly_dca.py dem LLM als ETF-Optionen anbietet.
REVOLUT_ETFS: dict[str, str] = {
    "VWCE.DE": "Vanguard FTSE All-World — weltweit, maximal breit gestreut",
    "SPYL.DE": "SPDR S&P 500 — US-Gesamtmarkt, breit gestreut",
    "EQQQ.DE": "Invesco Nasdaq-100 — Tech-lastig",
}

# Sektor-Zuordnung fuer das Momentum-Universum. Zweck: das LLM soll Klumpenrisiko
# gegen das bestehende Depot pruefen koennen ("schon 3 Halbleiter drin").
# Gruppen entsprechen den Kommentar-Bloecken in src/common/universe.py.
SECTORS: dict[str, str] = {
    **{t: "Tech" for t in (
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "ADBE", "CRM", "ORCL",
        "CSCO", "INTC", "IBM", "QCOM", "TXN", "AVGO", "MU", "AMD", "NFLX", "ACN", "HPQ")},
    **{t: "Financials" for t in (
        "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "PNC", "SCHW",
        "COF", "BLK", "BRK-B")},
    **{t: "Healthcare" for t in (
        "JNJ", "UNH", "PFE", "MRK", "ABBV", "TMO", "ABT", "LLY", "BMY", "AMGN",
        "GILD", "CVS", "MDT", "BIIB")},
    **{t: "Staples" for t in (
        "PG", "KO", "PEP", "WMT", "COST", "CL", "MO", "PM", "MDLZ", "KHC", "GIS", "KMB")},
    **{t: "Consumer" for t in (
        "HD", "MCD", "NKE", "SBUX", "LOW", "DIS", "BKNG", "F", "GM")},
    **{t: "Industrials" for t in (
        "BA", "HON", "UNP", "MMM", "GE", "CAT", "LMT", "DE", "FDX", "UPS", "EMR")},
    **{t: "Energy" for t in ("XOM", "CVX", "SLB", "COP", "OXY", "EOG", "KMI")},
    **{t: "Comm/Materials" for t in ("T", "VZ", "CMCSA", "LIN")},
    # Depot-Positionen ausserhalb des Momentum-Universums
    "ASML": "Tech",
    **{t: "ETF" for t in REVOLUT_ETFS},
}

# Sanity-Guards (identisch zur Live-Momentum-Strategie)
MAX_DAY_JUMP = 0.45     # groesserer Tagessprung = Split/Daten-Glitch
MAX_12M_MOM = 3.00      # >300% 12M bei Large-Cap = unrealistischer Datenmuell
_TD_MONTH = 21          # Handelstage je Monat (Naeherung)


def _pct(series, lookback: int) -> float | None:
    """Rendite ueber `lookback` Handelstage. None wenn Historie zu kurz."""
    if len(series) < lookback + 1:
        return None
    return float(series.iloc[-1] / series.iloc[-1 - lookback] - 1)


def metrics_for(ticker: str) -> dict | None:
    """Berechnet alle Kennzahlen fuer einen Ticker. None bei fehlenden/kaputten Daten."""
    try:
        px = get_prices(ticker, period="2y")
    except Exception as e:
        log.warning(f"{ticker}: Kursdaten nicht ladbar: {e}")
        return None
    if px is None or len(px) < _TD_MONTH * 7:   # min ~7 Monate Historie
        return None

    s = px["close"].dropna()
    if len(s) < _TD_MONTH * 7:
        return None

    # Sanity: Split/Glitch
    if float(s.pct_change().abs().max()) > MAX_DAY_JUMP:
        log.info(f"{ticker}: verworfen (Tagessprung > {MAX_DAY_JUMP:.0%} — Split/Glitch)")
        return None

    mom_1m = _pct(s, _TD_MONTH)
    mom_3m = _pct(s, _TD_MONTH * 3)
    mom_6m = _pct(s, _TD_MONTH * 6)
    mom_12m = _pct(s, _TD_MONTH * 12)

    if mom_6m is None:
        return None
    if mom_12m is not None and mom_12m > MAX_12M_MOM:
        log.info(f"{ticker}: verworfen (12M-Momentum {mom_12m:.0%} unrealistisch)")
        return None

    # 12-1-Momentum: Rendite von vor 12M bis vor 1M (letzter Monat ausgeklammert)
    mom_12_1 = None
    if len(s) >= _TD_MONTH * 12 + 1:
        mom_12_1 = float(s.iloc[-1 - _TD_MONTH] / s.iloc[-1 - _TD_MONTH * 12] - 1)

    ma200 = float(s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else None
    last = float(s.iloc[-1])
    vs_200d = (last / ma200 - 1) if ma200 else None

    high_52w = float(s.tail(_TD_MONTH * 12).max())
    dd = last / high_52w - 1 if high_52w else None

    rets = s.pct_change().dropna().tail(_TD_MONTH * 12)
    vol = float(rets.std() * np.sqrt(252)) if len(rets) > 20 else None

    return {
        "ticker":          ticker,
        "sector":          SECTORS.get(ticker, "?"),
        "price":           round(last, 2),
        "mom_1m":          round(mom_1m, 4) if mom_1m is not None else None,
        "mom_3m":          round(mom_3m, 4) if mom_3m is not None else None,
        "mom_6m":          round(mom_6m, 4),
        "mom_12m":         round(mom_12m, 4) if mom_12m is not None else None,
        "mom_12_1":        round(mom_12_1, 4) if mom_12_1 is not None else None,
        "vs_200d":         round(vs_200d, 4) if vs_200d is not None else None,
        "dd_from_52w_high": round(dd, 4) if dd is not None else None,
        "vol_annual":      round(vol, 4) if vol is not None else None,
        "trend_ok":        bool(vs_200d is not None and vs_200d > 0),
    }


def scan(tickers: list[str] | None = None) -> list[dict]:
    """Scannt eine Ticker-Liste (default: Momentum-Universum). Sortiert nach 6M-Momentum."""
    tickers = list(tickers if tickers is not None else UNIVERSE)
    out = []
    for tk in tickers:
        m = metrics_for(tk)
        if m:
            out.append(m)
    out.sort(key=lambda x: x["mom_6m"], reverse=True)
    coverage = len(out) / len(tickers) if tickers else 0.0
    log.info(f"Markt-Scan: {len(out)}/{len(tickers)} Ticker mit Daten ({coverage:.0%})")
    return out


def top_candidates(n: int = 15, require_trend: bool = True) -> list[dict]:
    """
    Top-N Kaufkandidaten aus dem breiten Universum.

    require_trend=True filtert Werte unter dem 200-Tage-Schnitt raus — fallende
    Werte sind fuer einen Buy-and-Hold-Sparplan ohne Rotationsregel die schlechteste
    Kombination (Momentum-Falle: was faellt, faellt oft weiter).
    Greift der Filter zu hart (weniger als n/3 uebrig = breiter Markt schwach),
    wird ohne Filter aufgefuellt und das im Feld `trend_ok` sichtbar gelassen.
    """
    universe = scan()
    picks = [c for c in universe if c["trend_ok"]] if require_trend else list(universe)
    if require_trend and len(picks) < max(1, n // 3):
        log.info("Trendfilter laesst zu wenig uebrig — fuelle mit Nicht-Trend-Werten auf")
        seen = {c["ticker"] for c in picks}
        picks += [c for c in universe if c["ticker"] not in seen]
    return picks[:n]


def etf_metrics() -> list[dict]:
    """Kennzahlen der drei Revolut-ETFs — als Vergleichsmassstab fuer jeden Einzeltitel."""
    out = []
    for tk, desc in REVOLUT_ETFS.items():
        m = metrics_for(tk)
        if m:
            m["description"] = desc
            out.append(m)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"{'Ticker':<8}{'Sektor':<16}{'6M':>8}{'12-1M':>8}{'vs200d':>9}{'DD52w':>8}{'Vola':>8}")
    print("-" * 65)
    def _f(v: float | None) -> str:
        return f"{v:+.1%}" if v is not None else "n/a"

    for c in top_candidates(20):
        print(f"{c['ticker']:<8}{c['sector']:<16}{_f(c['mom_6m']):>8}{_f(c['mom_12_1']):>8}"
              f"{_f(c['vs_200d']):>9}{_f(c['dd_from_52w_high']):>8}{_f(c['vol_annual']):>8}")
    print("\nETF-Vergleich:")
    for e in etf_metrics():
        print(f"  {e['ticker']:<9}6M {e['mom_6m']:+.1%}  12M {e['mom_12m']:+.1%}  "
              f"vs200d {e['vs_200d']:+.1%}")

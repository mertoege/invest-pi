#!/usr/bin/env python3
"""
universe.py — EINZIGE WAHRHEITSQUELLE fuer das Live-Momentum-Universum.

Aus dieser Liste waehlt die Live-Strategie (scripts/momentum_rebalance.py) monatlich
die Top-5 nach 6-Monats-Momentum. DIESELBE Liste nutzen die fairen Backtests
(scripts/champion_duell_fair.py & Co.), damit Backtest und Live deckungsgleich sind.

Regel statt These: breites, branchen-gestreutes 2018er-Large-Cap-Universum — BEWUSST
inkl. der bekannten Nieten (GE, INTC, IBM, F, T, PFE, BA, WBA…), damit KEIN Rueckschau-/
Sieger-Bias entsteht. Die Streuung ueber alle Sektoren ist gewollt: nur so kann Momentum
im Crash aus dem fallenden Sektor in einen steigenden rotieren (= geringerer Drawdown).

ACHTUNG: Wer hier Aktien aendert, aendert den ECHTEN (Paper-)Handel. NICHT verwechseln
mit config.yaml -> universe (Ring-Liste) — die steuert nur den Monats-Sparplan (DCA)
und die abgeschaltete Score-Engine, NICHT die Momentum-Kernstrategie.
"""
from __future__ import annotations

# Breites 2018er-Large-Cap-Universum — INKL. der bekannten Nieten der letzten 8 Jahre
UNIVERSE = [
    # Tech (Gewinner UND Verlierer)
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "ADBE", "CRM", "ORCL", "CSCO",
    "INTC", "IBM", "QCOM", "TXN", "AVGO", "MU", "AMD", "NFLX", "ACN", "HPQ",
    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "PNC", "SCHW", "COF", "BLK", "BRK-B",
    # Healthcare (inkl. Underperformer PFE/BMY/GILD/CVS/WBA)
    "JNJ", "UNH", "PFE", "MRK", "ABBV", "TMO", "ABT", "LLY", "BMY", "AMGN", "GILD", "CVS", "MDT", "BIIB",
    # Staples (inkl. KHC/MO/PM/WBA Verlierer)
    "PG", "KO", "PEP", "WMT", "COST", "CL", "MO", "PM", "MDLZ", "KHC", "GIS", "KMB",
    # Consumer Disc (inkl. F/GM/M Verlierer)
    "HD", "MCD", "NKE", "SBUX", "LOW", "DIS", "BKNG", "F", "GM",
    # Industrials (inkl. GE/BA/MMM Verlierer)
    "BA", "HON", "UNP", "MMM", "GE", "CAT", "LMT", "DE", "FDX", "UPS", "EMR",
    # Energy (lange schwach, 2022 stark)
    "XOM", "CVX", "SLB", "COP", "OXY", "EOG", "KMI",
    # Comm/Materials (inkl. T/VZ Verlierer)
    "T", "VZ", "CMCSA", "LIN",
]

# Benchmarks fuer Vergleiche (NICHT handelbar in der Strategie)
BENCH = ["SPY", "QQQ"]

#!/usr/bin/env python3
"""
build_patterns.py — Baut die Pattern-Library für deine Watchlist auf.

Einmal laufen lassen, dann quartals­weise wiederholen. Lädt 10 Jahre Historie
für jeden Ticker, extrahiert alle Drawdowns > 15 % und speichert die
Feature-Vektoren davor.

Laufzeit: ca. 30-60 Sekunden pro Ticker beim ersten Durchlauf (wegen Download).
Bei späteren Läufen fast instant, weil gecacht.

Usage:
    python scripts/build_patterns.py                 # Default-Watchlist
    python scripts/build_patterns.py NVDA AMD        # spezifische Ticker
    python scripts/build_patterns.py --summary-only  # nur Report, kein Mining
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pfad so setzen, dass Imports relativ zum Projekt-Root funktionieren
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.storage import init_all
from src.learning.pattern_miner import mine_ticker, summary


# ────────────────────────────────────────────────────────────
# DEINE WATCHLIST — aus § 02 des Plans
# ────────────────────────────────────────────────────────────
DEFAULT_WATCHLIST = [
    # Ring 1 — Core
    "NVDA", "ASML", "TSM", "AMD",
    # Ring 2 — Ecosystem
    "AVGO", "MRVL", "ARM", "CDNS", "SNPS", "KLAC", "LRCX",
    # Ring 3 — Hyperscaler & ETF
    "MSFT", "GOOGL", "META",
    "SMH", "SOXX",
]


def main() -> None:
    args = sys.argv[1:]

    if "--summary-only" in args:
        summary()
        return

    if "--help" in args or "-h" in args:
        print(__doc__)
        return

    tickers = [a for a in args if not a.startswith("--")] or DEFAULT_WATCHLIST

    print("=" * 60)
    print(f"  PATTERN LIBRARY BUILDER")
    print(f"  {len(tickers)} Ticker · 10 Jahre Historie")
    print("=" * 60)

    # Datenbanken initialisieren (idempotent)
    init_all()

    results = []
    for ticker in tickers:
        try:
            result = mine_ticker(ticker, period="10y")
            results.append(result)
        except Exception as e:
            print(f"   ❌ Fehler bei {ticker}: {e}")

    # Zusammenfassung
    print("\n" + "=" * 60)
    print("  DURCHGANG BEENDET")
    print("=" * 60)
    total_events = sum(r.get("events_found", 0) for r in results)
    total_saved  = sum(r.get("events_saved", 0) for r in results)
    print(f"  Gesamt: {total_events} Drawdowns gefunden, {total_saved} neu gespeichert")

    summary()


if __name__ == "__main__":
    main()

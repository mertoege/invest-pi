#!/usr/bin/env python3
"""
build_patterns.py — Pattern-Library Build/Refresh.

Modi:
  python scripts/build_patterns.py                  # alle Watchlist-Ticker (config.yaml universe)
  python scripts/build_patterns.py NVDA AMD         # spezifische Ticker
  python scripts/build_patterns.py --summary-only   # nur Report, kein Mining
  python scripts/build_patterns.py --missing-only   # nur Tickers ohne Events bisher

Wird von:
  - setup_pi.sh: einmal nach Initial-Setup
  - invest-pi-patterns.timer: monatlich Refresh
  - score_portfolio.py: silent-Bootstrap wenn DB leer (siehe ensure_patterns_built)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import config as cfg_mod
from src.common.storage import PATTERNS_DB, connect, init_all
from src.learning.pattern_miner import mine_ticker, summary


def existing_event_counts() -> dict[str, int]:
    """Returns {ticker: event_count} aus patterns.db."""
    try:
        with connect(PATTERNS_DB) as conn:
            rows = conn.execute(
                "SELECT ticker, COUNT(*) as n FROM drawdown_events GROUP BY ticker"
            ).fetchall()
        return {r["ticker"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def ensure_patterns_built(min_events_per_ticker: int = 5,
                          quiet: bool = True) -> dict:
    """
    Idempotenter Bootstrap. Prueft pro Ticker ob mind. X Events da sind,
    sonst wird mine_ticker() aufgerufen.

    Wird auch von score_portfolio.py aufgerufen (silent-mode).
    """
    init_all()
    cfg = cfg_mod.load()
    counts = existing_event_counts()

    target_tickers = cfg.watchlist_tickers
    missing = [t for t in target_tickers if counts.get(t, 0) < min_events_per_ticker]

    if not missing:
        return {"status": "all_present", "tickers_with_events": len(counts)}

    built, errors = 0, 0
    for ticker in missing:
        try:
            result = mine_ticker(ticker, period="10y")
            if "error" not in result:
                built += 1
        except Exception as e:
            errors += 1
            if not quiet:
                print(f"  Error mining {ticker}: {e}")
    return {
        "status": "built",
        "missing": len(missing),
        "built":   built,
        "errors":  errors,
    }


def main() -> None:
    args = sys.argv[1:]
    summary_only = "--summary-only" in args
    missing_only = "--missing-only" in args
    explicit = [a for a in args if not a.startswith("--")]

    init_all()

    if summary_only:
        summary()
        return

    cfg = cfg_mod.load()
    if explicit:
        tickers = [t.upper() for t in explicit]
    else:
        tickers = cfg.watchlist_tickers

    if missing_only:
        counts = existing_event_counts()
        before = len(tickers)
        tickers = [t for t in tickers if counts.get(t, 0) < 5]
        print(f"\n  --missing-only: {len(tickers)} of {before} tickers need mining")

    print(f"\n  Mining {len(tickers)} tickers...")
    built, errors = 0, 0
    for ticker in tickers:
        try:
            mine_ticker(ticker, period="10y")
            built += 1
        except Exception as e:
            print(f"  Error: {ticker}: {e}")
            errors += 1

    print(f"\n  Done: {built} built, {errors} errors")
    summary()


if __name__ == "__main__":
    main()

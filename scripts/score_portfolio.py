#!/usr/bin/env python3
"""
score_portfolio.py — Risk-Scoring für das gesamte Portfolio.

Nutzt jetzt config.yaml als einzige Quelle für Portfolio + Watchlist.
Neue Positionen einbuchen = eine Zeile in config.yaml, fertig.

Usage:
    python scripts/score_portfolio.py              # alle Portfolio-Positionen
    python scripts/score_portfolio.py NVDA         # ein Ticker
    python scripts/score_portfolio.py --with-patterns
    python scripts/score_portfolio.py --full-scan  # gesamtes Universe scannen
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.alerts.risk_scorer import score_ticker, print_report
from src.common import config as cfg_mod
from src.common.data_loader import get_prices
from src.common.storage import init_all
from src.learning.pattern_miner import compute_features, find_similar_patterns
from src.alerts.dispatch import dispatch_new_alerts


def main() -> None:
    args = sys.argv[1:]
    with_patterns = "--with-patterns" in args
    full_scan = "--full-scan" in args

    cfg = cfg_mod.load()
    init_all()

    explicit = [a for a in args if not a.startswith("--")]
    if explicit:
        tickers = explicit
        label = "Manuell angegeben"
    elif full_scan:
        tickers = cfg.watchlist_tickers
        label = f"Gesamtes Universe ({len(tickers)} Titel)"
    else:
        tickers = cfg.portfolio_tickers
        label = f"Portfolio ({len(tickers)} Positionen)"

    print("\n" + "=" * 62)
    print(f"  PORTFOLIO RISK SCAN · {label}")
    print("=" * 62)

    key_finnhub = cfg.api_keys.get("finnhub", "")
    key_news    = cfg.api_keys.get("newsapi", "")

    reports = []
    for ticker in tickers:
        entry = cfg.entry_by_ticker(ticker)
        if entry:
            print(f"\n  [Ring {entry.ring}] {entry.name} ({ticker})")
        try:
            report = score_ticker(ticker, key_finnhub or None, key_news or None)
            reports.append(report)
            print_report(report)
            if with_patterns:
                _print_analogs(ticker)
        except Exception as e:
            print(f"  Fehler: {e}")

    if reports:
        print("\n" + "=" * 62)
        print("  ZUSAMMENFASSUNG")
        print("=" * 62)
        for r in sorted(reports, key=lambda x: -x.composite):
            bar = chr(9608) * int(r.composite / 5)
            print(f"  {r.ticker:<8} {r.composite:5.1f}  {bar:<20} {r.alert_label}")

        alerts = [r for r in reports if r.alert_level >= 2]
        if alerts:
            print(f"\n  ALERT-POSITIONEN ({len(alerts)}):")
            for r in alerts:
                pos = cfg.portfolio.get(r.ticker)
                invested = f"{pos.invested_eur:.0f} EUR" if pos else "Watchlist"
                print(f"     Stufe {r.alert_level} · {r.ticker} · {invested}")
        else:
            print("\n  Keine aktiven Alerts.")

    _show_concentration(cfg)

    # ── Telegram-Dispatch fuer Stufe>=2 Alerts ────────────────
    try:
        stats = dispatch_new_alerts(lookback_hours=6)
        if not stats.get("skipped"):
            print(f"\n  Telegram: {stats['sent']} sent, {stats['failed']} failed "
                  f"(of {stats['candidates']} candidates, min_level={stats['min_level']})")
    except Exception as e:
        print(f"\n  Telegram-Dispatch fehlgeschlagen: {e}")


def _print_analogs(ticker: str, k: int = 3) -> None:
    try:
        prices = get_prices(ticker, period="2y")
        features = compute_features(prices, len(prices) - 1)
        if features is None:
            return
        matches = find_similar_patterns(features, lookback_days=7, top_k=k)
        if not matches:
            print("   (keine Pattern-Library-Daten)")
            return
        print(f"   Aehnlichste historische Setups:")
        for i, m in enumerate(matches, 1):
            print(f"      {i}. {m['ticker']} @ {m['peak_date']} "
                  f"-> {m['drawdown_pct']:+.1%} in {m['days_to_trough']}T ({m['regime']})")
    except Exception as e:
        print(f"   (pattern match error: {e})")


def _show_concentration(cfg) -> None:
    alloc = cfg.ring_allocation()
    total = sum(p.invested_eur for p in cfg.portfolio.values())
    if total == 0:
        return
    print("\n" + "-" * 62)
    print("  PORTFOLIO-KONZENTRATION")
    print("-" * 62)
    print(f"  Gesamt investiert: {total:.0f} EUR")
    for ticker, pos in sorted(cfg.portfolio.items(),
                               key=lambda x: -x[1].invested_eur):
        pct = pos.invested_eur / total
        bar = chr(9608) * int(pct * 40)
        warn = " !" if pct > cfg.settings.max_position_anteil * 0.85 else ""
        print(f"  {ticker:<8} {pos.invested_eur:6.0f} EUR  {pct:5.0%}  {bar}{warn}")
    print(f"\n  Ring-1: {alloc.get(1,0):.0%} / Ring-2: {alloc.get(2,0):.0%} / Ring-3: {alloc.get(3,0):.0%}")


if __name__ == "__main__":
    main()

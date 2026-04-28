#!/usr/bin/env python3
"""
buy.py — Neue Position einbuchen oder bestehende aufstocken.

Nach jedem Kauf im Broker rufst du dieses Script auf. Es:
  1. Prüft ob der Kauf die Konzentrations-Limits verletzt
  2. Aktualisiert config.yaml
  3. Zeigt neue Portfolio-Zusammensetzung

Usage:
    python scripts/buy.py NVDA 50              # 50 EUR in NVDA
    python scripts/buy.py MSFT 50 --shares 0.5 --price 420.00
    python scripts/buy.py --check NVDA 50      # nur prüfen, nicht einbuchen
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from src.common import config as cfg_mod

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--check" in args
    args = [a for a in args if not a.startswith("--")]

    if len(args) < 2:
        print(__doc__)
        return

    ticker     = args[0].upper()
    eur_amount = float(args[1])
    shares     = float(args[2]) if len(args) > 2 else None
    price      = float(args[3]) if len(args) > 3 else None

    cfg = cfg_mod.load()

    # Universum-Check: ist der Ticker überhaupt bekannt?
    entry = cfg.entry_by_ticker(ticker)
    if not entry:
        print(f"\n  Warnung: {ticker} ist nicht in config.yaml/universe definiert.")
        print(f"  Kannst du trotzdem hinzufügen — trag ihn in config.yaml ein.")

    # Konzentrations-Check
    check = cfg.concentration_check(ticker, eur_amount)
    print(f"\n  Konzentrations-Check für {ticker} + {eur_amount:.0f} EUR:")
    if check["blocks"]:
        print(f"  BLOCK:")
        for b in check["blocks"]:
            print(f"    ! {b}")
    if check["warnings"]:
        for w in check["warnings"]:
            print(f"    ~ {w}")
    if not check["blocks"] and not check["warnings"]:
        print(f"  OK — {check['ticker_pct_after']:.0%} des Portfolios")

    if dry_run:
        print("\n  Dry-run, keine Änderung an config.yaml.")
        return

    if check["blocks"]:
        print("\n  Kauf abgebrochen wegen Limit-Verletzung.")
        print("  Mit --check kannst du alternative Beträge testen.")
        return

    # Config aktualisieren
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    if "portfolio" not in raw:
        raw["portfolio"] = {}

    if ticker in raw["portfolio"]:
        existing = raw["portfolio"][ticker]
        old_invested = float(existing.get("invested_eur", 0))
        new_invested = old_invested + eur_amount

        # Durchschnitts-Preis neu berechnen
        if price and existing.get("avg_buy_price") and existing.get("shares"):
            old_shares = float(existing["shares"])
            new_shares = old_shares + (shares or eur_amount / price)
            new_avg = new_invested / new_shares
            existing["shares"] = round(new_shares, 6)
            existing["avg_buy_price"] = round(new_avg, 4)
        elif price and shares:
            existing["shares"] = round(
                (existing.get("shares") or 0) + shares, 6
            )
            existing["avg_buy_price"] = price

        existing["invested_eur"] = round(new_invested, 2)
        print(f"\n  {ticker}: {old_invested:.0f} EUR → {new_invested:.0f} EUR aufgestockt")
    else:
        # Neue Position
        ring = entry.ring if entry else 0
        raw["portfolio"][ticker] = {
            "invested_eur":    round(eur_amount, 2),
            "shares":          round(shares, 6) if shares else None,
            "avg_buy_price":   round(price, 4) if price else None,
            "date_first":      _this_month(),
            "currency":        _guess_currency(ticker),
            "ring":            ring,
            "note":            entry.note if entry else "",
        }
        print(f"\n  {ticker}: neue Position {eur_amount:.0f} EUR")

    CONFIG_PATH.write_text(yaml.dump(raw, allow_unicode=True, default_flow_style=False))
    cfg_mod.reload()
    print(f"  config.yaml aktualisiert.")


def _this_month() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m")


def _guess_currency(ticker: str) -> str:
    if ticker.endswith(".DE") or ticker.endswith(".PA") or ticker.endswith(".AS"):
        return "EUR"
    return "USD"


if __name__ == "__main__":
    main()

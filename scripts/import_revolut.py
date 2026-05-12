#!/usr/bin/env python3
"""
import_revolut.py — Revolut Trading CSV → config.yaml Portfolio-Sync.

Liest Revolut-Statement-CSVs aus _inbox/ und aktualisiert die
Portfolio-Sektion in config.yaml mit echten Positionen.

Revolut CSV Format:
    Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate

Usage:
    python scripts/import_revolut.py                    # alle CSVs in _inbox/
    python scripts/import_revolut.py statement.csv      # bestimmte Datei
    python scripts/import_revolut.py --dry-run           # nur anzeigen, nicht schreiben
"""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.yaml"
INBOX_DIR = REPO_ROOT / "_inbox"
ARCHIVE_DIR = INBOX_DIR / "imported"

RELEVANT_TYPES = {"BUY - MARKET", "BUY - LIMIT", "SELL - MARKET", "SELL - LIMIT"}
DIVIDEND_TYPES = {"DIVIDEND"}
SPLIT_TYPES = {"STOCK SPLIT"}


@dataclass
class Trade:
    date: datetime
    ticker: str
    action: str  # "buy" or "sell"
    quantity: float
    price_per_share: float
    total_amount: float
    currency: str
    fx_rate: float


@dataclass
class HoldingSummary:
    ticker: str
    shares: float = 0.0
    total_invested_eur: float = 0.0
    total_invested_native: float = 0.0
    currency: str = "USD"
    date_first: str = ""
    dividends_native: float = 0.0
    trades: list[Trade] = field(default_factory=list)

    @property
    def avg_buy_price(self) -> float | None:
        if self.shares <= 0:
            return None
        buy_cost = sum(t.total_amount for t in self.trades if t.action == "buy")
        buy_shares = sum(t.quantity for t in self.trades if t.action == "buy")
        return buy_cost / buy_shares if buy_shares > 0 else None

    @property
    def invested_eur(self) -> float:
        eur = 0.0
        for t in self.trades:
            amount_eur = abs(t.total_amount) / t.fx_rate if t.fx_rate > 0 else abs(t.total_amount)
            if t.action == "buy":
                eur += amount_eur
            elif t.action == "sell":
                eur -= amount_eur
        return max(0.0, eur)


def _parse_amount(val: str) -> float:
    """Parse '$85.11' or '€88,94' or '85.11' to float."""
    if not val or not val.strip():
        return 0.0
    cleaned = val.strip().replace("$", "").replace("€", "").replace("£", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_quantity(val: str) -> float:
    if not val or not val.strip():
        return 0.0
    cleaned = val.strip().replace('"', '').replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_fx(val: str) -> float:
    if not val or not val.strip():
        return 1.0
    try:
        return float(val.strip())
    except ValueError:
        return 1.0


def _parse_date(val: str) -> datetime:
    val = val.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(val[:26].rstrip("Z") + "Z" if "Z" in val else val, fmt)
        except ValueError:
            continue
    # Fallback: try dateutil
    try:
        from dateutil.parser import parse
        return parse(val)
    except Exception:
        raise ValueError(f"Kann Datum nicht parsen: {val!r}")


def parse_revolut_csv(filepath: Path) -> list[Trade]:
    """Parse eine Revolut Trading Statement CSV."""
    trades: list[Trade] = []

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError(f"Leere CSV: {filepath}")

        # Normalize headers (strip whitespace)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]

        expected = {"Date", "Ticker", "Type"}
        if not expected.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"Unerwartete CSV-Spalten: {reader.fieldnames}\n"
                f"Erwartet mindestens: {expected}"
            )

        for row in reader:
            trade_type = row.get("Type", "").strip().upper()
            ticker = row.get("Ticker", "").strip()

            if not ticker:
                continue

            if trade_type in SPLIT_TYPES:
                # Stock splits werden als separate Quantity-Aenderung behandelt
                qty = _parse_quantity(row.get("Quantity", ""))
                if qty != 0:
                    trades.append(Trade(
                        date=_parse_date(row["Date"]),
                        ticker=ticker,
                        action="split_adjustment",
                        quantity=qty,
                        price_per_share=0.0,
                        total_amount=0.0,
                        currency=row.get("Currency", "USD").strip(),
                        fx_rate=_parse_fx(row.get("FX Rate", "")),
                    ))
                continue

            if trade_type in DIVIDEND_TYPES:
                trades.append(Trade(
                    date=_parse_date(row["Date"]),
                    ticker=ticker,
                    action="dividend",
                    quantity=0,
                    price_per_share=0.0,
                    total_amount=_parse_amount(row.get("Total Amount", "")),
                    currency=row.get("Currency", "USD").strip(),
                    fx_rate=_parse_fx(row.get("FX Rate", "")),
                ))
                continue

            # BUY / SELL
            is_buy = "BUY" in trade_type
            is_sell = "SELL" in trade_type
            if not (is_buy or is_sell):
                continue

            trades.append(Trade(
                date=_parse_date(row["Date"]),
                ticker=ticker,
                action="buy" if is_buy else "sell",
                quantity=_parse_quantity(row.get("Quantity", "")),
                price_per_share=_parse_amount(row.get("Price per share", "")),
                total_amount=_parse_amount(row.get("Total Amount", "")),
                currency=row.get("Currency", "USD").strip(),
                fx_rate=_parse_fx(row.get("FX Rate", "")),
            ))

    return sorted(trades, key=lambda t: t.date)


def aggregate_holdings(trades: list[Trade]) -> dict[str, HoldingSummary]:
    """Aggregiere Trades zu aktuellen Holdings."""
    holdings: dict[str, HoldingSummary] = {}

    for trade in trades:
        if trade.ticker not in holdings:
            holdings[trade.ticker] = HoldingSummary(
                ticker=trade.ticker,
                currency=trade.currency,
            )
        h = holdings[trade.ticker]

        if trade.action == "buy":
            h.shares += trade.quantity
            h.trades.append(trade)
            if not h.date_first or trade.date.strftime("%Y-%m") < h.date_first:
                h.date_first = trade.date.strftime("%Y-%m")

        elif trade.action == "sell":
            h.shares -= trade.quantity
            h.trades.append(trade)

        elif trade.action == "split_adjustment":
            h.shares += trade.quantity

        elif trade.action == "dividend":
            h.dividends_native += trade.total_amount

    # Nur Positionen mit shares > 0 behalten
    return {t: h for t, h in holdings.items() if h.shares > 0.001}


def update_config_yaml(holdings: dict[str, HoldingSummary], dry_run: bool = False) -> dict:
    """Aktualisiert die portfolio-Sektion in config.yaml."""
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    existing_portfolio = raw.get("portfolio", {}) or {}

    changes = {"added": [], "updated": [], "removed": [], "unchanged": []}

    new_portfolio = {}
    for ticker, h in sorted(holdings.items()):
        old = existing_portfolio.get(ticker)
        entry = {
            "invested_eur": round(h.invested_eur, 2),
            "shares": round(h.shares, 6),
            "avg_buy_price": round(h.avg_buy_price, 4) if h.avg_buy_price else None,
            "date_first": h.date_first,
            "currency": h.currency,
            "ring": old.get("ring", 2) if old else 2,
            "note": old.get("note", f"Revolut DCA") if old else "Revolut DCA",
        }
        new_portfolio[ticker] = entry

        if old is None:
            changes["added"].append(ticker)
        elif (round(old.get("invested_eur", 0), 2) != entry["invested_eur"]
              or round(old.get("shares") or 0, 6) != entry["shares"]):
            changes["updated"].append(ticker)
        else:
            changes["unchanged"].append(ticker)

    # Positionen die in config.yaml sind aber nicht in Revolut —
    # behalten (koennte Alpaca Paper-Trading sein)
    for ticker, old_entry in existing_portfolio.items():
        if ticker not in new_portfolio:
            new_portfolio[ticker] = old_entry
            note = old_entry.get("note", "")
            if "Revolut" not in note:
                changes["unchanged"].append(ticker)
            else:
                changes["removed"].append(ticker)

    if not dry_run and (changes["added"] or changes["updated"] or changes["removed"]):
        raw["portfolio"] = new_portfolio
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return changes


def find_revolut_csvs() -> list[Path]:
    """Finde alle Revolut-CSVs in _inbox/."""
    if not INBOX_DIR.exists():
        return []
    csvs = []
    for p in sorted(INBOX_DIR.glob("*.csv")):
        try:
            with open(p, encoding="utf-8-sig") as f:
                header = f.readline()
            if "Ticker" in header and "Type" in header:
                csvs.append(p)
        except Exception:
            continue
    return csvs


def archive_csv(filepath: Path) -> None:
    """Verschiebe verarbeitete CSV nach _inbox/imported/."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / filepath.name
    if dest.exists():
        stem = filepath.stem
        suffix = filepath.suffix
        dest = ARCHIVE_DIR / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
    filepath.rename(dest)
    print(f"  Archiviert: {dest.name}")


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    explicit = [a for a in args if not a.startswith("--")]

    if explicit:
        csv_files = [Path(p) if Path(p).is_absolute() else INBOX_DIR / p for p in explicit]
    else:
        csv_files = find_revolut_csvs()

    if not csv_files:
        print("Keine Revolut-CSVs gefunden.")
        print(f"  Lege dein Revolut-Statement als CSV in: {INBOX_DIR}/")
        print("  (Revolut App → Stocks → Mehr → Kontoauszug → CSV)")
        return

    print("=" * 62)
    print("  REVOLUT PORTFOLIO IMPORT")
    print("=" * 62)

    all_trades: list[Trade] = []
    for csv_file in csv_files:
        if not csv_file.exists():
            print(f"  Datei nicht gefunden: {csv_file}")
            continue
        print(f"\n  Parsing: {csv_file.name}")
        trades = parse_revolut_csv(csv_file)
        buys = sum(1 for t in trades if t.action == "buy")
        sells = sum(1 for t in trades if t.action == "sell")
        divs = sum(1 for t in trades if t.action == "dividend")
        print(f"    {len(trades)} Eintraege: {buys} Buys, {sells} Sells, {divs} Dividenden")
        all_trades.extend(trades)

    if not all_trades:
        print("\n  Keine relevanten Trades gefunden.")
        return

    holdings = aggregate_holdings(all_trades)

    print(f"\n  Aktive Positionen: {len(holdings)}")
    print("-" * 62)
    for ticker, h in sorted(holdings.items()):
        avg = f"@ {h.avg_buy_price:.2f}" if h.avg_buy_price else ""
        divs = f" (+{h.dividends_native:.2f} Div)" if h.dividends_native > 0 else ""
        print(f"  {ticker:<8} {h.shares:>10.4f} Shares  {h.invested_eur:>8.2f} EUR  {avg}{divs}")

    if dry_run:
        print("\n  [DRY RUN] config.yaml wird NICHT aktualisiert.")
        changes = update_config_yaml(holdings, dry_run=True)
    else:
        changes = update_config_yaml(holdings, dry_run=False)

    print(f"\n  Aenderungen:")
    if changes["added"]:
        print(f"    + Neu:        {', '.join(changes['added'])}")
    if changes["updated"]:
        print(f"    ~ Aktualisiert: {', '.join(changes['updated'])}")
    if changes["removed"]:
        print(f"    - Entfernt:   {', '.join(changes['removed'])}")
    if changes["unchanged"]:
        print(f"    = Unveraendert: {', '.join(changes['unchanged'])}")

    if not dry_run and (changes["added"] or changes["updated"]):
        print(f"\n  config.yaml aktualisiert!")

    # CSV archivieren (nicht bei dry-run)
    if not dry_run:
        for csv_file in csv_files:
            if csv_file.exists():
                archive_csv(csv_file)


if __name__ == "__main__":
    main()

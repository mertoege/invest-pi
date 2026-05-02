#!/usr/bin/env python3
"""
weekly_recap.py — Sonntag-Abend Trading-Recap via Telegram.

Verschickt eine uebersichtliche Wochen-Zusammenfassung:
  1. Portfolio-Performance (Equity-Veraenderung)
  2. Trades der Woche (Buys + Sells mit Ergebnis)
  3. Hit-Rate-Update (7d vs 30d)
  4. Aktive Positionen mit unrealized P&L
  5. Regime-Status
  6. Naechste Woche: Earnings-Kalender

Wird von invest-pi-weekly-recap.timer (Sonntag 19:00 CEST) ausgefuehrt.

Usage:
    python scripts/weekly_recap.py
    python scripts/weekly_recap.py --dry-run    # print statt Telegram
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts import notifier
from src.common.storage import TRADING_DB, LEARNING_DB, connect, init_all
from src.common.predictions import hit_rate_stratified


def _equity_change(days: int = 7, source: str = "paper") -> dict:
    """Equity-Veraenderung ueber N Tage."""
    sql = """
        SELECT total_eur, total_usd, timestamp
          FROM equity_snapshots
         WHERE source = ?
           AND timestamp >= datetime('now', ?)
         ORDER BY timestamp
    """
    with connect(TRADING_DB) as conn:
        rows = conn.execute(sql, (source, f"-{days} day")).fetchall()
    if len(rows) < 2:
        return {"available": False}
    first = rows[0]
    last = rows[-1]
    start_eur = float(first["total_eur"])
    end_eur = float(last["total_eur"])
    change_eur = end_eur - start_eur
    change_pct = (change_eur / start_eur) if start_eur > 0 else 0
    return {
        "available": True,
        "start_eur": round(start_eur, 2),
        "end_eur": round(end_eur, 2),
        "change_eur": round(change_eur, 2),
        "change_pct": round(change_pct, 4),
    }


def _trades_this_week(days: int = 7, source: str = "paper") -> list[dict]:
    """Trades der letzten N Tage."""
    sql = """
        SELECT ticker, side, qty, eur_value, price, status, strategy_label, notes, created_at
          FROM trades
         WHERE source = ?
           AND created_at >= datetime('now', ?)
           AND status = 'filled'
         ORDER BY created_at
    """
    with connect(TRADING_DB) as conn:
        rows = conn.execute(sql, (source, f"-{days} day")).fetchall()
    return [dict(r) for r in rows]


def _active_positions(source: str = "paper") -> list[dict]:
    """Aktive Positionen aus DB."""
    sql = """
        SELECT ticker, qty, avg_price_eur, strategy_label, opened_at, peak_price
          FROM positions
         WHERE source = ?
           AND qty > 0
         ORDER BY ticker
    """
    with connect(TRADING_DB) as conn:
        rows = conn.execute(sql, (source,)).fetchall()
    return [dict(r) for r in rows]


def _current_regime() -> str:
    """Aktuelles Regime-Label."""
    try:
        from src.learning.regime import current_regime
        r = current_regime()
        return f"{r.label} ({r.method}, p={r.probability:.0%})"
    except Exception:
        return "unknown"


def _upcoming_earnings(days: int = 7) -> list[str]:
    """Earnings der naechsten Woche fuer gehaltene Positionen."""
    positions = _active_positions()
    upcoming = []
    for p in positions:
        try:
            from src.alerts.earnings import get_next_earnings_date
            ed = get_next_earnings_date(p["ticker"])
            if ed:
                days_until = (ed - dt.date.today()).days
                if 0 <= days_until <= days:
                    upcoming.append(f"{p['ticker']}: {ed.isoformat()} ({days_until}d)")
        except Exception:
            continue
    return upcoming


def build_recap(source: str = "paper") -> str:
    """Baut die Recap-Nachricht als HTML-String."""
    parts = ["<b>Weekly Trading Recap</b>"]
    parts.append(f"<i>{dt.date.today().isoformat()}</i>\n")

    # 1. Equity
    eq = _equity_change(7, source)
    if eq["available"]:
        arrow = "+" if eq["change_eur"] >= 0 else ""
        parts.append("<b>Portfolio</b>")
        parts.append(
            f"  {eq['start_eur']:.2f} -> {eq['end_eur']:.2f} EUR "
            f"({arrow}{eq['change_eur']:.2f}, {arrow}{eq['change_pct']:.1%})\n"
        )

    # 2. Trades
    trades = _trades_this_week(7, source)
    if trades:
        buys = [t for t in trades if t["side"] == "buy"]
        sells = [t for t in trades if t["side"] == "sell"]
        parts.append(f"<b>Trades</b> ({len(trades)} total)")
        for t in trades[:10]:
            arrow = "BUY" if t["side"] == "buy" else "SELL"
            val = f"{t['eur_value']:.0f} EUR" if t["eur_value"] else ""
            note = f" ({t['notes'][:40]})" if t.get("notes") else ""
            parts.append(f"  {arrow} {t['ticker']} x{t['qty']:.0f} {val}{note}")
        parts.append("")
    else:
        parts.append("<b>Trades</b>: keine diese Woche\n")

    # 3. Hit-Rate
    rates = hit_rate_stratified("daily_score", days=7)
    o7 = rates["overall"]
    rates30 = hit_rate_stratified("daily_score", days=30)
    o30 = rates30["overall"]
    parts.append("<b>Hit-Rate</b>")
    if o7["measured"] > 0:
        parts.append(f"  7d:  {o7['correct']}/{o7['measured']} ({(o7['hit_rate'] or 0):.0%})")
    if o30["measured"] > 0:
        parts.append(f"  30d: {o30['correct']}/{o30['measured']} ({(o30['hit_rate'] or 0):.0%})")
    parts.append("")

    # 4. Positionen
    positions = _active_positions(source)
    if positions:
        parts.append(f"<b>Positionen</b> ({len(positions)} offen)")
        for p in positions[:8]:
            strat = p.get("strategy_label") or ""
            parts.append(f"  {p['ticker']} x{p['qty']:.0f}  [{strat}]")
        if len(positions) > 8:
            parts.append(f"  ... +{len(positions)-8} weitere")
        parts.append("")

    # 5. Regime
    parts.append(f"<b>Regime</b>: {_current_regime()}\n")

    # 6. Earnings naechste Woche
    earnings = _upcoming_earnings(7)
    if earnings:
        parts.append("<b>Earnings naechste Woche</b>")
        for e in earnings:
            parts.append(f"  {e}")
        parts.append("")

    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", default="paper")
    args = parser.parse_args()

    init_all()
    recap = build_recap(args.source)

    if args.dry_run:
        # Strip HTML tags for terminal readability
        import re
        clean = re.sub(r'<[^>]+>', '', recap)
        print(clean)
        return

    if notifier.is_configured():
        ok = notifier.send_info(recap, label="weekly_recap")
        print(f"Telegram: {'sent' if ok else 'FAILED'}")
    else:
        print("Telegram not configured, printing recap:")
        print(recap)


if __name__ == "__main__":
    main()

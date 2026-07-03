#!/usr/bin/env python3
"""
ai_swing_sync.py — Broker->DB Sync + Equity-Snapshot fuer das ZWEITE Alpaca-Paper-Konto.

Spiegelt scripts/sync_positions.py, betrifft aber ausschliesslich das KI-Swing-System
auf dem separaten Paper-Account (Keys ALPACA_API_KEY_2 / ALPACA_API_SECRET_2).

WICHTIG — Ledger-Trennung:
  Beide Konten sind PAPER, also ist is_paper bei BEIDEN True. Das Quell-Tag darf
  deshalb NIEMALS aus is_paper abgeleitet werden. Dieses Script schreibt IMMER
  fest source="ai_swing" — sonst wuerde der Momentum-Ledger (source="paper")
  korrumpiert. Das Tag wird ueberall EXPLIZIT durchgereicht.

Wird stuendlich vom systemd-Timer (invest-pi-ai-swing-sync.timer, :40) aufgerufen.
Idempotent: schreibt pro Lauf genau einen Equity-Snapshot und gleicht die
positions-Tabelle (source='ai_swing') an den Broker-Stand an.

Usage:
    python scripts/ai_swing_sync.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── .env laden (Standalone-Script braucht ALPACA_API_KEY_2/_SECRET_2) ─────────
_env = Path(__file__).resolve().parents[1] / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.broker import get_broker
from src.common.fx import eur_per_usd
from src.common.storage import TRADING_DB, connect, init_all

# Festes Quell-Tag — NIE aus is_paper ableiten (beide Konten sind paper).
SOURCE = "ai_swing"


def sync() -> dict:
    """Liest das 2. Paper-Konto und schreibt Equity-Snapshot + Positions (source='ai_swing')."""
    # Zweite Broker-Instanz explizit mit den _2-Keys bauen.
    broker = get_broker(
        "alpaca_paper",
        api_key=os.environ["ALPACA_API_KEY_2"],
        api_secret=os.environ["ALPACA_API_SECRET_2"],
    )

    # 1. Account + Positionen vom Broker (Source of Truth)
    account = broker.get_account()
    positions = broker.get_positions()
    pos_value = sum(p.market_value_eur for p in positions)

    with connect(TRADING_DB) as conn:
        # 2. Equity-Snapshot mit USD + FX — source EXPLIZIT "ai_swing"
        positions_value_usd = (
            sum(p.market_value_eur for p in positions) / account.fx_rate
            if account.fx_rate else 0
        )
        conn.execute(
            """
            INSERT INTO equity_snapshots
                (cash_eur, positions_value_eur, total_eur,
                 cash_usd, positions_value_usd, total_usd,
                 fx_rate, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account.cash_eur, pos_value, account.equity_eur,
             account.cash_usd, positions_value_usd, account.equity_usd,
             account.fx_rate, SOURCE),
        )

        # 3. Positions upsert auf (ticker, source) — Broker = Wahrheit fuer qty/avg,
        #    andere DB-Felder (stop_loss, peak_price, ...) bleiben erhalten.
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        broker_tickers = {p.ticker for p in positions}

        # Verwaiste Positionen (in DB, nicht mehr beim Broker) entfernen — nur ai_swing.
        existing = {r["ticker"] for r in conn.execute(
            "SELECT ticker FROM positions WHERE source = ?", (SOURCE,)
        ).fetchall()}
        for tk in (existing - broker_tickers):
            conn.execute(
                "DELETE FROM positions WHERE ticker = ? AND source = ?",
                (tk, SOURCE),
            )

        for p in positions:
            avg_eur = p.avg_price * eur_per_usd()
            row = conn.execute(
                "SELECT ticker FROM positions WHERE ticker = ? AND source = ?",
                (p.ticker, SOURCE),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE positions
                       SET qty = ?, avg_price_eur = ?, last_updated = ?
                     WHERE ticker = ? AND source = ?""",
                    (p.qty, avg_eur, now, p.ticker, SOURCE),
                )
            else:
                conn.execute(
                    """INSERT INTO positions
                        (ticker, qty, avg_price_eur, opened_at, last_updated,
                         strategy_label, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (p.ticker, p.qty, avg_eur, now, now, "ai_swing-v1", SOURCE),
                )

        # 4. Order-Status abgleichen: 'accepted' -> 'filled'/'rejected'. Eigener Reconciler,
        #    weil der Momentum-sync_orders auf Konto 1 laeuft und diese Order-IDs (Konto 2)
        #    dort nie findet. Fail-soft bei Lookup-Fehler (nicht-terminal lassen).
        pending = conn.execute(
            "SELECT rowid AS rid, broker_order_id FROM trades "
            "WHERE source = ? AND status NOT IN ('filled','cancelled','rejected','expired') "
            "AND broker_order_id IS NOT NULL AND broker_order_id != ''",
            (SOURCE,),
        ).fetchall()
        n_reconciled = 0
        for r in pending:
            try:
                o = broker.get_order(r["broker_order_id"])
            except Exception:
                continue
            if o.status == "filled":
                conn.execute(
                    "UPDATE trades SET status='filled', fill_ts=?, fill_price=? WHERE rowid=?",
                    (o.filled_at or now, o.avg_fill_price, r["rid"]),
                )
                n_reconciled += 1
            elif o.status in ("cancelled", "rejected", "expired"):
                conn.execute("UPDATE trades SET status=? WHERE rowid=?", (o.status, r["rid"]))
                n_reconciled += 1

    return {
        "broker":     str(broker),
        "cash_eur":   account.cash_eur,
        "equity_eur": account.equity_eur,
        "positions":  len(positions),
        "reconciled": n_reconciled,
        "source":     SOURCE,
    }


def main() -> None:
    init_all()
    result = sync()
    print(f"  broker:    {result['broker']}")
    print(f"  cash:      {result['cash_eur']:>10.2f} EUR")
    print(f"  equity:    {result['equity_eur']:>10.2f} EUR")
    print(f"  positions: {result['positions']}")
    print(f"  reconciled:{result['reconciled']:>3} Order(s) -> terminal")
    print(f"  source:    {result['source']}")


if __name__ == "__main__":
    main()

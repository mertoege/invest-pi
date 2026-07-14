#!/usr/bin/env python3
"""
sync_positions.py — Broker -> DB Sync + Equity-Snapshot.

Wird stuendlich vom systemd-Timer aufgerufen.

Usage:
    python scripts/sync_positions.py             # default broker aus config.yaml
    python scripts/sync_positions.py --mock      # Mock-Broker (nur fuer Tests)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.broker import get_broker
from src.common.fx import eur_per_usd
from src.common.storage import TRADING_DB, connect, init_all
from src.trading import load_trading_config


def sync(broker_kind: str | None = None) -> dict:
    cfg = load_trading_config()
    kind = broker_kind or cfg.broker
    broker = get_broker(kind)
    src = "paper" if broker.is_paper else "live"

    # 1. Account-Snapshot
    account = broker.get_account()
    positions = broker.get_positions()
    pos_value = sum(p.market_value_eur for p in positions)

    # GLITCH-WAECHTER (Fix 2026-07-14): Alpaca liefert gelegentlich kurz 0 Positionen
    # (noch nicht geladen), obwohl das Depot bestueckt ist. Ungeschuetzt hat der Sync
    # dann (a) einen Muell-Equity-Snapshot "equity = nur Cash" geschrieben (verzerrt
    # Peak/Drawdown/Performance und loeste am 2026-07-07 einen -98%-Fehlalarm-Kill aus)
    # und (b) ALLE Positionen aus der DB geloescht (opened_at ging verloren). Die
    # Momentum-Engine liquidiert nie automatisch auf 0 -> ein leerer Broker-Read bei
    # bestuecktem Depot ist praktisch immer ein Datenfehler. Diesen Zyklus ueberspringen.
    if not positions:
        with connect(TRADING_DB) as conn:
            db_pos = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE source = ?", (src,)
            ).fetchone()[0]
        if db_pos > 0:
            print(f"  WARN: Broker meldet 0 Positionen, DB haelt {db_pos} (source={src}) "
                  f"-> Datenfehler vermutet, Sync-Zyklus uebersprungen (kein Snapshot, keine Loeschung).")
            return {"broker": str(broker), "cash_eur": account.cash_eur,
                    "equity_eur": account.equity_eur, "positions": 0,
                    "source": src, "skipped": "empty_positions_glitch"}

    with connect(TRADING_DB) as conn:
        # 2. Equity-Snapshot mit USD + FX
        positions_value_usd = sum(p.market_value_eur for p in positions) / account.fx_rate if account.fx_rate else 0
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
             account.fx_rate, src),
        )

        # 3. Positions upsert (broker = source of truth fuer qty/avg, DB-Felder bleiben erhalten)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        broker_tickers = {p.ticker for p in positions}

        existing = {r["ticker"] for r in conn.execute(
            "SELECT ticker FROM positions WHERE source = ?", (src,)
        ).fetchall()}
        for tk in (existing - broker_tickers):
            conn.execute("DELETE FROM positions WHERE ticker = ? AND source = ?", (tk, src))

        for p in positions:
            avg_eur = p.avg_price * eur_per_usd()
            row = conn.execute(
                "SELECT ticker FROM positions WHERE ticker = ? AND source = ?",
                (p.ticker, src),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE positions
                       SET qty = ?, avg_price_eur = ?, last_updated = ?
                     WHERE ticker = ? AND source = ?""",
                    (p.qty, avg_eur, now, p.ticker, src),
                )
            else:
                conn.execute(
                    """INSERT INTO positions
                        (ticker, qty, avg_price_eur, opened_at, last_updated, source)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (p.ticker, p.qty, avg_eur, now, now, src),
                )

    return {
        "broker":     str(broker),
        "cash_eur":   account.cash_eur,
        "equity_eur": account.equity_eur,
        "positions":  len(positions),
        "source":     src,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true",
                        help="Mock-Broker statt Default")
    args = parser.parse_args()

    init_all()
    result = sync(broker_kind="mock" if args.mock else None)
    print(f"  broker:    {result['broker']}")
    print(f"  cash:      {result['cash_eur']:>10.2f} EUR")
    print(f"  equity:    {result['equity_eur']:>10.2f} EUR")
    print(f"  positions: {result['positions']}")
    print(f"  source:    {result['source']}")


if __name__ == "__main__":
    main()

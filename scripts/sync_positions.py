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

    with connect(TRADING_DB) as conn:
        # 2. Equity-Snapshot
        conn.execute(
            """
            INSERT INTO equity_snapshots
                (cash_eur, positions_value_eur, total_eur, source)
            VALUES (?, ?, ?, ?)
            """,
            (account.cash_eur, pos_value, account.equity_eur, src),
        )

        # 3. Positions ueberschreiben (broker = source of truth)
        conn.execute("DELETE FROM positions WHERE source = ?", (src,))
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for p in positions:
            conn.execute(
                """
                INSERT INTO positions
                    (ticker, qty, avg_price_eur, opened_at, last_updated, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (p.ticker, p.qty, p.avg_price * eur_per_usd(), now, now, src),
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

#!/usr/bin/env python3
"""
sync_orders.py — Synchronisiert Order-Status von Alpaca in die lokale trades-DB.

Problem: run_strategy.py platziert Orders und speichert den initialen Status
(oft 'pending_new' ausserhalb Market Hours). Wenn Alpaca die Order spaeter
fuellt, weiss die lokale DB nichts davon.

Dieses Script:
  1. Findet alle trades mit status != 'filled' und != 'cancelled'
  2. Fragt bei Alpaca den aktuellen Status ab
  3. Updated die lokale DB (status, fill_ts, fill_price)

Laeuft als Teil von run_strategy.py (am Anfang) und kann auch standalone laufen.

Usage:
    python scripts/sync_orders.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt
import os

env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.broker import get_broker
from src.common.storage import TRADING_DB, connect, init_all
from src.trading import load_trading_config


def sync_order_statuses() -> dict:
    """Synchronisiert pending Orders mit Alpaca. Returns summary dict."""
    t_cfg = load_trading_config()
    broker = get_broker(t_cfg.broker)

    with connect(TRADING_DB) as conn:
        pending = conn.execute(
            """
            SELECT rowid AS rid, broker_order_id, ticker, side, qty
              FROM trades
             WHERE status NOT IN ('filled', 'cancelled', 'rejected', 'expired')
               AND broker_order_id IS NOT NULL
               AND broker_order_id != ''
            """
        ).fetchall()

    if not pending:
        return {"synced": 0, "still_pending": 0, "errors": 0}

    synced = 0
    still_pending = 0
    errors = 0

    for row in pending:
        try:
            order = broker.get_order(row["broker_order_id"])
        except Exception:
            errors += 1
            continue

        if order.status == "filled":
            with connect(TRADING_DB) as conn:
                conn.execute(
                    """
                    UPDATE trades
                       SET status = 'filled',
                           fill_ts = ?,
                           fill_price = ?
                     WHERE rowid = ?
                    """,
                    (
                        order.filled_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        order.avg_fill_price,
                        row["rid"],
                    ),
                )
            synced += 1
        elif order.status in ("cancelled", "rejected", "expired"):
            with connect(TRADING_DB) as conn:
                conn.execute(
                    "UPDATE trades SET status = ? WHERE rowid = ?",
                    (order.status, row["rid"]),
                )
            synced += 1
        else:
            still_pending += 1

    return {"synced": synced, "still_pending": still_pending, "errors": errors}


def main():
    init_all()
    result = sync_order_statuses()
    print(f"Order-Sync: {result['synced']} aktualisiert, "
          f"{result['still_pending']} noch pending, "
          f"{result['errors']} Fehler")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
run_strategy.py — Main orchestrator fuer autonomes Paper-Trading.

Pipeline:
  1. Init DBs
  2. Load config + trading-config + universe (Ring 1 + 2 als tradeable)
  3. Get broker (default aus config.yaml: alpaca_paper oder mock)
  4. sync_positions (broker = source of truth fuer was wir halten)
  5. Stop-loss-pass: positions die <= -stop_loss_pct sind, verkaufen
  6. For each tradeable ticker, der NICHT gehalten ist:
       - decide_action -> wenn buy:
       - pre_trade_check (kill_switch, market_hours, max_trades_per_day, ...)
       - size_position
       - broker.place_order
       - trade-row eintragen, position aktualisiert sich beim naechsten sync
  7. Final equity_snapshot

Usage:
    python scripts/run_strategy.py
    python scripts/run_strategy.py --dry-run    # keine Orders, nur Decisions printen
    python scripts/run_strategy.py --mock       # MockBroker statt config-broker
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.broker import get_broker, BrokerAdapter
from src.common import config as cfg_mod
from src.common.storage import TRADING_DB, connect, init_all
from src.risk.limits import pre_trade_check, positions_to_stop_loss
from src.trading import TradingConfig, load_trading_config
from src.trading.decision import decide_action, log_decision
from src.trading.sizing import size_position


def _record_trade(
    *,
    decision_pred_id: int | None,
    ticker: str, side: str, qty: float, eur_value: float,
    price: float, status: str, order_id: str,
    strategy_label: str, source: str, notes: str = "",
) -> None:
    with connect(TRADING_DB) as conn:
        conn.execute(
            """
            INSERT INTO trades
                (ticker, side, qty, eur_value, price, order_type, status,
                 broker_order_id, strategy_label, prediction_id,
                 fill_ts, fill_price, source, notes)
            VALUES (?, ?, ?, ?, ?, 'market', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, side, qty, eur_value, price, status, order_id,
             strategy_label, decision_pred_id,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") if status == "filled" else None,
             price if status == "filled" else None,
             source, notes),
        )


def _take_equity_snapshot(broker: BrokerAdapter, source: str, notes: str = "") -> None:
    acc = broker.get_account()
    pos_val = sum(p.market_value_eur for p in broker.get_positions())
    with connect(TRADING_DB) as conn:
        conn.execute(
            """
            INSERT INTO equity_snapshots
                (cash_eur, positions_value_eur, total_eur, source, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (acc.cash_eur, pos_val, acc.equity_eur, source, notes),
        )


def stop_loss_pass(broker: BrokerAdapter, t_cfg: TradingConfig, source: str, dry_run: bool) -> int:
    """Verkauft alle Positionen unter stop_loss_pct. Returns count of sells."""
    triggered = positions_to_stop_loss(broker, t_cfg)
    if not triggered:
        return 0
    for ticker, qty, price in triggered:
        print(f"  STOP-LOSS {ticker}: -{t_cfg.stop_loss_pct:.0%} reached, sell {qty} @ {price:.2f}")
        if dry_run:
            continue
        result = broker.place_order(ticker=ticker, side="sell", qty=qty)
        _record_trade(
            decision_pred_id=None,
            ticker=ticker, side="sell", qty=qty,
            eur_value=qty * price * 0.92, price=price,
            status=result.status, order_id=result.order_id,
            strategy_label="stop_loss-v1", source=source,
            notes=f"auto stop-loss at {-t_cfg.stop_loss_pct:.0%}",
        )
    return len(triggered)


def buy_pass(broker: BrokerAdapter, cfg, t_cfg: TradingConfig, source: str, dry_run: bool) -> dict:
    """Pruefe alle tradeable Tickers, treffe Decision, fuehre Buys aus."""
    held = {p.ticker for p in broker.get_positions()}
    open_n = len(held)
    candidates = [e for e in cfg.universe if e.ring in t_cfg.tradeable_rings]

    decisions = {"buys": [], "skips": [], "errors": []}
    fx = 0.92

    for entry in candidates:
        # Stoppe wenn wir das Tages-Limit fuer Trades erreichen
        check = pre_trade_check(broker, t_cfg)
        if not check.allowed:
            decisions["errors"].append({"ticker": entry.ticker, "reason": check.reason})
            print(f"  GLOBAL STOP: {check.reason}")
            break

        decision = decide_action(
            ticker=entry.ticker,
            held_tickers=held,
            open_positions_count=open_n,
            ring=entry.ring,
            config=t_cfg,
        )
        log_decision(decision, strategy_label=f"{t_cfg.mode}-v1")

        if decision.action != "buy":
            decisions["skips"].append({"ticker": entry.ticker, "reason": decision.reason})
            continue

        # Sizing
        quote = broker.get_quote(entry.ticker)
        sz = size_position(decision, broker.get_account().cash_eur, quote.last, fx, t_cfg)
        if sz.skip:
            decisions["skips"].append({"ticker": entry.ticker, "reason": f"sizing: {sz.skip_reason}"})
            continue

        if dry_run:
            print(f"  BUY (dry-run) {entry.ticker}: {sz.qty} @ {quote.last:.2f} = {sz.eur_amount:.2f} EUR  ({decision.reason})")
            decisions["buys"].append({"ticker": entry.ticker, "qty": sz.qty, "dry_run": True})
            continue

        result = broker.place_order(ticker=entry.ticker, side="buy", qty=sz.qty)
        _record_trade(
            decision_pred_id=decision.decision_pred_id,
            ticker=entry.ticker, side="buy", qty=sz.qty,
            eur_value=sz.eur_amount, price=quote.last,
            status=result.status, order_id=result.order_id,
            strategy_label=f"{t_cfg.mode}-v1", source=source,
            notes=decision.reason,
        )
        print(f"  BUY {entry.ticker}: {sz.qty} @ {quote.last:.2f} = {sz.eur_amount:.2f} EUR  [{result.status}]")
        decisions["buys"].append({
            "ticker": entry.ticker, "qty": sz.qty,
            "status": result.status, "order_id": result.order_id,
        })

        if result.status == "filled":
            held.add(entry.ticker)
            open_n += 1
            if open_n >= t_cfg.max_open_positions:
                print(f"  max_open_positions reached, stopping for today")
                break

    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Decisions berechnen und printen, keine Orders.")
    parser.add_argument("--mock", action="store_true",
                        help="Mock-Broker statt config.yaml-broker.")
    parser.add_argument("--skip-stop-loss", action="store_true")
    parser.add_argument("--skip-buys", action="store_true")
    args = parser.parse_args()

    init_all()
    cfg = cfg_mod.load()
    t_cfg = load_trading_config()

    if not t_cfg.enabled:
        print("trading.enabled=false in config.yaml — nothing to do.")
        return

    broker = get_broker("mock" if args.mock else t_cfg.broker)
    src = "paper" if broker.is_paper else "live"
    print(f"\n=== run_strategy · broker={broker} · mode={t_cfg.mode} · source={src} ===")

    # Initial snapshot
    _take_equity_snapshot(broker, src, notes="run_strategy:start")

    if not args.skip_stop_loss:
        n = stop_loss_pass(broker, t_cfg, src, args.dry_run)
        print(f"  stop-loss pass: {n} sells")

    if not args.skip_buys:
        decisions = buy_pass(broker, cfg, t_cfg, src, args.dry_run)
        print(f"\n  buys:  {len(decisions['buys'])}")
        print(f"  skips: {len(decisions['skips'])}")
        if decisions['errors']:
            print(f"  errors: {len(decisions['errors'])}")

    _take_equity_snapshot(broker, src, notes="run_strategy:end")
    final = broker.get_account()
    print(f"\n  Final equity: {final.equity_eur:.2f} EUR  (cash {final.cash_eur:.2f})")


if __name__ == "__main__":
    main()

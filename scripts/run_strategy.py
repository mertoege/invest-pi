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
from src.alerts import notifier
from src.common import config as cfg_mod
from src.common.fx import eur_per_usd
from src.common.storage import TRADING_DB, connect, init_all
from src.risk.limits import (
    pre_trade_check, positions_to_stop_loss,
    positions_to_take_profit, positions_to_trailing_stop,
    cash_floor_check, sector_concentration_check, correlation_check,
)
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
    positions = broker.get_positions()
    pos_val_eur = sum(p.market_value_eur for p in positions)
    pos_val_usd = pos_val_eur / acc.fx_rate if acc.fx_rate else 0
    with connect(TRADING_DB) as conn:
        conn.execute(
            """
            INSERT INTO equity_snapshots
                (cash_eur, positions_value_eur, total_eur,
                 cash_usd, positions_value_usd, total_usd,
                 fx_rate, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (acc.cash_eur, pos_val_eur, acc.equity_eur,
             acc.cash_usd, pos_val_usd, acc.equity_usd,
             acc.fx_rate, source, notes),
        )


def _update_peak_prices(broker: BrokerAdapter, source: str) -> int:
    """Update peak_price fuer alle Positionen. Muss jeden Run laufen."""
    updated = 0
    with connect(TRADING_DB) as conn:
        for pos in broker.get_positions():
            row = conn.execute(
                "SELECT peak_price FROM positions WHERE ticker = ? AND source = ?",
                (pos.ticker, source),
            ).fetchone()
            if row is None:
                continue
            old_peak = row["peak_price"] or 0
            new_peak = max(old_peak, pos.market_price)
            if new_peak > old_peak:
                conn.execute(
                    "UPDATE positions SET peak_price = ?, peak_seen_at = datetime('now'), last_updated = datetime('now') WHERE ticker = ? AND source = ?",
                    (new_peak, pos.ticker, source),
                )
                updated += 1
    return updated


def _update_position_after_buy(ticker: str, strategy_label: str, source: str, price: float) -> None:
    """Setzt strategy_label und initialisiert peak_price nach einem Buy."""
    with connect(TRADING_DB) as conn:
        conn.execute(
            """UPDATE positions
               SET strategy_label = ?,
                   peak_price = CASE WHEN peak_price IS NULL OR peak_price < ? THEN ? ELSE peak_price END,
                   last_updated = datetime('now')
             WHERE ticker = ? AND source = ?""",
            (strategy_label, price, price, ticker, source),
        )


def take_profit_pass(broker, t_cfg, source: str, dry_run: bool) -> int:
    """Verkauft Positionen >= +take_profit_pct."""
    triggered = positions_to_take_profit(broker, t_cfg)
    if not triggered:
        return 0
    for ticker, qty, price in triggered:
        gain_pct = ((price / next((p.avg_price for p in broker.get_positions() if p.ticker == ticker), price)) - 1.0)
        print(f"  TAKE-PROFIT {ticker}: +{gain_pct:.0%} reached, sell {qty} @ {price:.2f}")
        if dry_run:
            continue
        result = broker.place_order(ticker=ticker, side="sell", qty=qty)
        _record_trade(
            decision_pred_id=None,
            ticker=ticker, side="sell", qty=qty,
            eur_value=qty * price * 0.92, price=price,
            status=result.status, order_id=result.order_id,
            strategy_label="take_profit-v1", source=source,
            notes=f"auto take-profit at +{t_cfg.take_profit_pct:.0%}",
        )
        if result.status == "filled":
            try:
                from src.alerts import notifier
                notifier.send_trade(
                    ticker=ticker, side="sell", qty=qty,
                    eur=qty * price * 0.92, price_usd=price,
                    reason=f"TAKE-PROFIT at +{t_cfg.take_profit_pct:.0%}",
                    paper=broker.is_paper,
                )
            except Exception as e:
                print(f"  notifier failed: {e}")
    return len(triggered)


def trailing_stop_pass(broker, t_cfg, source: str, dry_run: bool) -> int:
    """Verkauft Positionen die vom Hoch um trailing_stop_pct gefallen sind."""
    # Peak-prices aus DB holen
    from src.common.storage import TRADING_DB, connect
    with connect(TRADING_DB) as conn:
        peaks = {
            r["ticker"]: r["peak_price"]
            for r in conn.execute(
                "SELECT ticker, peak_price FROM positions WHERE source = ?", (source,)
            ).fetchall()
            if r["peak_price"] is not None
        }

    triggered = positions_to_trailing_stop(broker, t_cfg, peaks)
    if not triggered:
        return 0
    for ticker, qty, price, peak in triggered:
        drawdown = ((price / peak) - 1.0)
        print(f"  TRAILING-STOP {ticker}: {drawdown:+.0%} from peak {peak:.2f}, sell {qty} @ {price:.2f}")
        if dry_run:
            continue
        result = broker.place_order(ticker=ticker, side="sell", qty=qty)
        _record_trade(
            decision_pred_id=None,
            ticker=ticker, side="sell", qty=qty,
            eur_value=qty * price * 0.92, price=price,
            status=result.status, order_id=result.order_id,
            strategy_label="trailing_stop-v1", source=source,
            notes=f"trailing-stop {drawdown:+.0%} from peak {peak:.2f}",
        )
        if result.status == "filled":
            try:
                from src.alerts import notifier
                notifier.send_trade(
                    ticker=ticker, side="sell", qty=qty,
                    eur=qty * price * 0.92, price_usd=price,
                    reason=f"TRAILING-STOP {drawdown:+.0%} from peak",
                    paper=broker.is_paper,
                )
            except Exception as e:
                print(f"  notifier failed: {e}")
    return len(triggered)


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
            eur_value=qty * price * eur_per_usd(), price=price,
            status=result.status, order_id=result.order_id,
            strategy_label="stop_loss-v1", source=source,
            notes=f"auto stop-loss at {-t_cfg.stop_loss_pct:.0%}",
        )
        if result.status == "filled":
            try:
                notifier.send_trade(
                    ticker=ticker, side="sell", qty=qty,
                    eur=qty * price * eur_per_usd(), price_usd=price,
                    reason=f"STOP-LOSS at {-t_cfg.stop_loss_pct:.0%}",
                    paper=broker.is_paper,
                )
            except Exception as e:
                print(f"  notifier failed: {e}")
    return len(triggered)


def buy_pass(broker: BrokerAdapter, cfg, t_cfg: TradingConfig, source: str, dry_run: bool) -> dict:
    """Pruefe alle tradeable Tickers, treffe Decision, fuehre Buys aus."""
    from src.trading import get_active_profile
    held = {p.ticker for p in broker.get_positions()}
    open_n = len(held)
    candidates = [e for e in cfg.universe if e.ring in t_cfg.tradeable_rings]

    # Regime-aware: Sektoren filtern + priorisieren
    profile = get_active_profile(t_cfg)
    sector_pref = profile.get("sector_preference", [])
    sector_avoid = profile.get("sector_avoid", [])
    target_invest_pct = profile.get("target_invest_pct", 0.50)

    if sector_avoid:
        before = len(candidates)
        candidates = [e for e in candidates
                      if _get_sector_for_ticker(t_cfg, e.ticker) not in sector_avoid]
        skipped = before - len(candidates)
        if skipped:
            print(f"  regime-filter: {skipped} Ticker in vermiedenen Sektoren uebersprungen")

    if sector_pref:
        preferred = [e for e in candidates if _get_sector_for_ticker(t_cfg, e.ticker) in sector_pref]
        others = [e for e in candidates if _get_sector_for_ticker(t_cfg, e.ticker) not in sector_pref]
        candidates = preferred + others

    # Cash-Ziel: nicht mehr investieren als target_invest_pct vom Equity
    account = broker.get_account()
    current_invested = sum(p.market_value_eur for p in broker.get_positions())
    max_invest = account.equity_eur * target_invest_pct
    budget_left = max(0, max_invest - current_invested)

    decisions = {"buys": [], "skips": [], "errors": []}
    fx = eur_per_usd()

    if budget_left < t_cfg.min_position_eur:
        print(f"  regime-budget: {current_invested:.0f}/{max_invest:.0f} EUR "
              f"({target_invest_pct:.0%} Ziel) -- kein Budget")
        return decisions

    print(f"  regime-budget: {budget_left:.0f} EUR verfuegbar "
          f"({current_invested:.0f}/{max_invest:.0f} EUR, Ziel {target_invest_pct:.0%})")

    # Korrelations-Check vorbereiten
    corr_check_available = False
    try:
        from src.learning.correlation import correlation_check_for_buy
        corr_check_available = True
    except Exception:
        pass

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

        # Pre-Buy: cash_floor + sector_concentration
        cash_ok, cash_reason = cash_floor_check(broker, t_cfg)
        if not cash_ok:
            decisions["skips"].append({"ticker": entry.ticker, "reason": f"cash-floor: {cash_reason}"})
            continue
        sector_ok, sector_reason = sector_concentration_check(broker, t_cfg, entry.ticker, sz.eur_amount)
        if not sector_ok:
            decisions["skips"].append({"ticker": entry.ticker, "reason": f"sector-cap: {sector_reason}"})
            continue
        corr_ok, corr_reason = correlation_check(broker, entry.ticker)
        if not corr_ok:
            decisions["skips"].append({"ticker": entry.ticker, "reason": f"correlation: {corr_reason}"})
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
            strategy_label=f"{t_cfg.mode}-{decision.strategy_label}-v1", source=source,
            notes=decision.reason,
        )
        if result.status == "filled":
            _update_position_after_buy(
                entry.ticker,
                f"{t_cfg.mode}-{decision.strategy_label}-v1",
                source, quote.last,
            )
        print(f"  BUY {entry.ticker}: {sz.qty} @ {quote.last:.2f} = {sz.eur_amount:.2f} EUR  [{result.status}]")
        decisions["buys"].append({
            "ticker": entry.ticker, "qty": sz.qty,
            "status": result.status, "order_id": result.order_id,
        })

        if result.status in ("filled", "pending_new", "accepted"):
            held.add(entry.ticker)
            open_n += 1
            if open_n >= t_cfg.max_open_positions:
                print(f"  max_open_positions reached, stopping for today")
                break

    return decisions


def _get_sector_for_ticker(t_cfg, ticker: str) -> str | None:
    sector_map = getattr(t_cfg, "sector_map", {}) or {}
    for sector, tickers in sector_map.items():
        if ticker in tickers:
            return sector
    return None


def regime_rebalance_pass(
    broker: BrokerAdapter, t_cfg, regime, transition: dict, source: str, dry_run: bool,
) -> int:
    """Bei Regime-Wechsel: Positionen in sector_avoid verkaufen + Investitionsquote anpassen."""
    from src.trading import get_active_profile
    profile = get_active_profile(t_cfg)
    sector_avoid = profile.get("sector_avoid", [])
    target_invest_pct = profile.get("target_invest_pct", 0.50)

    positions = broker.get_positions()
    account = broker.get_account()
    current_invested = sum(p.market_value_eur for p in positions)
    target_invested = account.equity_eur * target_invest_pct
    sells = 0

    if sector_avoid:
        for p in positions:
            sector = _get_sector_for_ticker(t_cfg, p.ticker)
            if sector and sector in sector_avoid:
                print(f"  REGIME-SELL {p.ticker}: Sektor \'{sector}\' vermieden in {regime.label}")
                if dry_run:
                    sells += 1
                    continue
                result = broker.place_order(ticker=p.ticker, side="sell", qty=p.qty)
                _record_trade(
                    decision_pred_id=None,
                    ticker=p.ticker, side="sell", qty=p.qty,
                    eur_value=p.market_value_eur, price=p.market_price,
                    status=result.status, order_id=result.order_id,
                    strategy_label="regime_rebalance-v1", source=source,
                    notes=f"regime {transition[\'from\']}->{transition[\'to\']}: avoid {sector}",
                )
                if result.status == "filled":
                    try:
                        notifier.send_trade(
                            ticker=p.ticker, side="sell", qty=p.qty,
                            eur=p.market_value_eur, price_usd=p.market_price,
                            reason=f"Regime->{regime.label}: {sector} reduziert",
                            paper=broker.is_paper,
                        )
                    except Exception:
                        pass
                sells += 1

    if current_invested > target_invested * 1.2:
        excess = current_invested - target_invested
        sorted_pos = sorted(positions, key=lambda p: p.market_value_eur)
        for p in sorted_pos:
            sector = _get_sector_for_ticker(t_cfg, p.ticker)
            if sector and sector in sector_avoid:
                continue
            if excess <= 0:
                break
            print(f"  REGIME-REDUCE {p.ticker}: {p.market_value_eur:.0f} EUR "
                  f"(invest {current_invested:.0f}/{target_invested:.0f} EUR Ziel)")
            if dry_run:
                excess -= p.market_value_eur
                sells += 1
                continue
            result = broker.place_order(ticker=p.ticker, side="sell", qty=p.qty)
            _record_trade(
                decision_pred_id=None,
                ticker=p.ticker, side="sell", qty=p.qty,
                eur_value=p.market_value_eur, price=p.market_price,
                status=result.status, order_id=result.order_id,
                strategy_label="regime_rebalance-v1", source=source,
                notes=f"regime rebalance: reduce to {target_invest_pct:.0%} target",
            )
            excess -= p.market_value_eur
            sells += 1

    return sells


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

    # Apply pending config patches from meta-review
    try:
        from src.learning.config_patcher import apply_trading_patches
        applied = apply_trading_patches(t_cfg)
        if applied:
            print(f"  Applied {len(applied)} config patches from meta-review:")
            for a in applied:
                print(f"    {a}")
    except Exception as e:
        print(f"  config patch application skipped: {e}")

    broker = get_broker("mock" if args.mock else t_cfg.broker)
    src = "paper" if broker.is_paper else "live"

    # Regime-Check + Transition
    regime = None
    transition = None
    try:
        from src.learning.regime import current_regime, detect_regime_transition
        regime = current_regime()
        transition = detect_regime_transition()
        regime_de = {"low_vol_bull": "Bullish", "high_vol_mixed": "Volatil", "bear": "Baerisch"}.get(regime.label, regime.label)
        print(f"\n=== run_strategy · broker={broker} · mode={t_cfg.mode} · source={src} ===")
        print(f"  regime: {regime_de} ({regime.probability:.0%}, {regime.method})")
        if transition:
            print(f"  REGIME-WECHSEL: {transition['from']} -> {transition['to']}")
            try:
                notifier.send_info(
                    f"<b>Regime-Wechsel</b>\n"
                    f"{transition['from']} -> <b>{transition['to']}</b> ({regime.probability:.0%})",
                    label="regime_transition",
                )
            except Exception:
                pass
    except Exception as e:
        print(f"\n=== run_strategy · broker={broker} · mode={t_cfg.mode} · source={src} ===")
        print(f"  regime check failed: {e}")

    # Sync pending orders from previous runs
    try:
        from scripts.sync_orders import sync_order_statuses
        sync = sync_order_statuses()
        if sync["synced"] > 0:
            print(f"  order-sync: {sync['synced']} Orders aktualisiert")
    except Exception as e:
        print(f"  order-sync skipped: {e}")

    # Initial snapshot + Peak-Price-Update
    _take_equity_snapshot(broker, src, notes="run_strategy:start")
    n_peaks = _update_peak_prices(broker, src)
    if n_peaks:
        print(f"  peak-prices updated: {n_peaks}")

    if not args.skip_stop_loss:
        n_tp = take_profit_pass(broker, t_cfg, src, args.dry_run)
        print(f"  take-profit pass: {n_tp} sells")
        n_tr = trailing_stop_pass(broker, t_cfg, src, args.dry_run)
        print(f"  trailing-stop pass: {n_tr} sells")
        n = stop_loss_pass(broker, t_cfg, src, args.dry_run)
        print(f"  stop-loss pass: {n} sells")

    # Regime-Rebalancing bei Wechsel
    if transition:
        n_rb = regime_rebalance_pass(broker, t_cfg, regime, transition, src, args.dry_run)
        if n_rb:
            print(f"  regime-rebalance: {n_rb} sells")

    if not args.skip_buys:
        decisions = buy_pass(broker, cfg, t_cfg, src, args.dry_run)
        print(f"\n  buys:  {len(decisions['buys'])}")
        print(f"  skips: {len(decisions['skips'])}")
        if decisions['errors']:
            print(f"  errors: {len(decisions['errors'])}")

        # Top-Up untergewichtete Positionen wenn keine neuen Buys
        if not decisions['buys']:
            try:
                from scripts.weekly_rotation import topup_pass
                from src.trading import get_active_profile
                profile = get_active_profile(t_cfg)
                topups = topup_pass(broker, t_cfg, profile, src, args.dry_run)
                if topups:
                    print(f"  top-ups: {len(topups)}")
            except Exception as e:
                print(f"  top-up skipped: {e}")

    _take_equity_snapshot(broker, src, notes="run_strategy:end")
    final = broker.get_account()
    print(f"\n  Final equity: {final.equity_eur:.2f} EUR  (cash {final.cash_eur:.2f})")


if __name__ == "__main__":
    main()

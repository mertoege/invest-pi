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
import fcntl
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


def _sell_with_limit(broker, ticker, qty, bid=0, last=0, **kw):
    if bid > 0:
        lp = round(bid * 0.999, 2)
    elif last > 0:
        lp = round(last * 0.998, 2)
    else:
        return broker.place_order(ticker=ticker, side="sell", qty=qty, **kw)
    return broker.place_order(ticker=ticker, side="sell", qty=qty,
                              order_type="limit", limit_price=lp, **kw)


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
    spy_close = None
    try:
        from src.common.data_loader import get_prices
        spy = get_prices("SPY", period="5d")
        if spy is not None and len(spy) > 0:
            spy_close = float(spy["close"].iloc[-1])
    except Exception:
        pass
    with connect(TRADING_DB) as conn:
        conn.execute(
            """
            INSERT INTO equity_snapshots
                (cash_eur, positions_value_eur, total_eur,
                 cash_usd, positions_value_usd, total_usd,
                 fx_rate, source, notes, spy_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (acc.cash_eur, pos_val_eur, acc.equity_eur,
             acc.cash_usd, pos_val_usd, acc.equity_usd,
             acc.fx_rate, source, notes, spy_close),
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
    """Setzt strategy_label, peak_price und stop_loss_price nach einem Buy."""
    t_cfg = load_trading_config()
    from src.trading import get_active_profile
    profile = get_active_profile(t_cfg) or {}
    sl_pct = profile.get("stop_loss_pct", t_cfg.stop_loss_pct)
    stop_price = round(price * (1 - sl_pct), 4)
    with connect(TRADING_DB) as conn:
        conn.execute(
            """UPDATE positions
               SET strategy_label = ?,
                   peak_price = CASE WHEN peak_price IS NULL OR peak_price < ? THEN ? ELSE peak_price END,
                   stop_loss_price = CASE WHEN stop_loss_price IS NULL THEN ? ELSE stop_loss_price END,
                   last_updated = datetime('now')
             WHERE ticker = ? AND source = ?""",
            (strategy_label, price, price, stop_price, ticker, source),
        )


def take_profit_pass(broker, t_cfg, source: str, dry_run: bool) -> int:
    """3-Tier gestaffeltes Profit-Taking + Full-TP."""
    triggered = positions_to_take_profit(broker, t_cfg)
    if not triggered:
        return 0
    avg_prices = {p.ticker: p.avg_price for p in broker.get_positions() if p.avg_price > 0}
    for ticker, qty, price, tp_type in triggered:
        gain_pct = ((price / avg_prices.get(ticker, price)) - 1.0)
        is_full = tp_type == "full"
        label = "TAKE-PROFIT" if is_full else tp_type.upper().replace("_", "-")
        print(f"  {label} {ticker}: +{gain_pct:.0%} reached, sell {qty} @ {price:.2f}")
        if dry_run:
            continue
        result = _sell_with_limit(broker, ticker, qty, last=price)
        strategy = "take_profit-v1" if is_full else f"{tp_type}-v1"
        _record_trade(
            decision_pred_id=None,
            ticker=ticker, side="sell", qty=qty,
            eur_value=qty * price * eur_per_usd(), price=price,
            status=result.status, order_id=result.order_id,
            strategy_label=strategy, source=source,
            notes=f"{label} at +{gain_pct:.0%}",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            try:
                notifier.send_trade(
                    ticker=ticker, side="sell", qty=qty,
                    eur=qty * price * eur_per_usd(), price_usd=price,
                    reason=f"{label} at +{gain_pct:.0%}",
                    paper=broker.is_paper,
                )
            except Exception:
                pass
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
        result = _sell_with_limit(broker, ticker, qty, last=price)
        _record_trade(
            decision_pred_id=None,
            ticker=ticker, side="sell", qty=qty,
            eur_value=qty * price * eur_per_usd(), price=price,
            status=result.status, order_id=result.order_id,
            strategy_label="trailing_stop-v1", source=source,
            notes=f"trailing-stop {drawdown:+.0%} from peak {peak:.2f}",
        )
        if result.status == "filled":
            try:
                from src.alerts import notifier
                notifier.send_trade(
                    ticker=ticker, side="sell", qty=qty,
                    eur=qty * price * eur_per_usd(), price_usd=price,
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
        result = _sell_with_limit(broker, ticker, qty, last=price)
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


def _risk_sell_cooldown(ticker: str, hours: int = 12) -> bool:
    """True wenn in den letzten N Stunden bereits ein Risk-Sell stattfand."""
    try:
        with connect(TRADING_DB) as conn:
            row = conn.execute(
                """SELECT 1 FROM trades
                   WHERE ticker=? AND strategy_label LIKE 'risk_sell_%'
                     AND created_at > datetime('now', ?)
                   LIMIT 1""",
                (ticker, f"-{hours} hours"),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def risk_sell_pass(broker: BrokerAdapter, t_cfg: TradingConfig, source: str, dry_run: bool) -> int:
    """Verkauft Positionen basierend auf Risk-Score. Regime-aware: im Bull weniger aggressiv."""
    from src.trading.decision import latest_risk_score, risk_signal_predictive
    # Autonomes Signal-Validitaets-Gate: nur auf Warnungen handeln wenn das
    # Warnsignal nachweislich Drawdowns vorhersagt. Sonst zerstoeren Risk-Sells
    # Rendite (anti-praediktive Fehlalarme). Stop-Loss/Trailing/Take-Profit sind
    # davon UNBERUEHRT (preisbasiert, kein Vorhersage-Signal).
    _sig_ok, _sig_reason = risk_signal_predictive()
    if not _sig_ok:
        print(f"  risk-sell pass: 0 sells ({_sig_reason})")
        return 0
    # Regime-aware Schwellen: im Bull-Markt weniger verkaufen
    regime_label = "unknown"
    try:
        from src.learning.regime import current_regime
        regime = current_regime()
        if regime.probability >= 0.55:
            regime_label = regime.label
    except Exception:
        pass

    positions = broker.get_positions()
    sells = 0
    for pos in positions:
        score = latest_risk_score(pos.ticker)
        if not score or score["alert_level"] < 2:
            continue
        if _risk_sell_cooldown(pos.ticker, hours=48):
            continue
        # Regime-aware Schwellen: Attribution zeigt dass Risk-Signale
        # in Bull/Mixed-Maerkten kontraer wirken → konservativer verkaufen
        if regime_label == "low_vol_bull":
            if score["alert_level"] >= 3 and score["composite"] >= 85:
                sell_pct, label = 0.30, "RED"
            else:
                continue
        elif regime_label == "high_vol_mixed":
            if score["alert_level"] >= 3 and score["composite"] >= 80:
                sell_pct, label = 0.50, "RED"
            elif score["composite"] >= 75 and score["triggered_n"] >= 6:
                sell_pct, label = 0.25, "CAUTION"
            else:
                continue
        else:
            # Bear oder unbekannt: aggressiver Risk-Management
            if score["alert_level"] >= 3:
                sell_pct, label = 1.0, "RED"
            elif score["composite"] >= 65:
                sell_pct, label = 0.5, "CAUTION"
            else:
                continue
        sell_qty = round(pos.qty * sell_pct, 4)
        if sell_qty <= 0:
            continue
        sell_eur = pos.market_value_eur * sell_pct
        print(f"  RISK-SELL {pos.ticker}: {label} alert (composite={score['composite']:.1f}, "
              f"{score['triggered_n']} triggers), sell {sell_pct:.0%} = {sell_qty} @ {pos.market_price:.2f}")
        if dry_run:
            sells += 1
            continue
        result = _sell_with_limit(broker, pos.ticker, sell_qty, last=pos.market_price)
        _record_trade(
            decision_pred_id=None,
            ticker=pos.ticker, side="sell", qty=sell_qty,
            eur_value=sell_eur, price=pos.market_price,
            status=result.status, order_id=result.order_id,
            strategy_label=f"risk_sell_{label.lower()}-v1", source=source,
            notes=f"{label} alert: composite={score['composite']:.1f}, sell {sell_pct:.0%}",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            sells += 1
    return sells



# ETF-Proxies fuer Sektor-Momentum
_SECTOR_ETF_MAP = {
    "technology": "XLK", "financials": "XLF", "energy": "XLE",
    "healthcare": "XLV", "industrials": "XLI", "consumer_staples": "XLP",
    "consumer_disc": "XLY", "utilities": "XLU", "real_estate": "XLRE",
    "communication": "XLC", "materials": "XLB", "software": "XLK",
    "etfs": None,
}

def _sector_momentum_map() -> dict[str, float]:
    """Berechnet 20d-Momentum pro Sektor via ETF-Proxy. Cached pro Run."""
    from src.common.data_loader import get_prices
    result = {}
    for sector, etf in _SECTOR_ETF_MAP.items():
        if not etf or sector in result:
            continue
        try:
            prices = get_prices(etf, period="1mo")
            if prices is not None and len(prices) >= 5:
                result[sector] = float(prices["close"].iloc[-1] / prices["close"].iloc[0] - 1)
            else:
                result[sector] = 0.0
        except Exception:
            result[sector] = 0.0
    result["software"] = result.get("technology", 0.0)
    result["etfs"] = 0.0
    return result


def _derisk_cooldown(source: str, hours: int = 48) -> bool:
    """True wenn in den letzten N Stunden bereits entrisikt wurde (verhindert
    eine Verkaufs-Todesspirale, wenn das Depot mehrere Runs im Drawdown bleibt)."""
    try:
        with connect(TRADING_DB) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE strategy_label LIKE 'derisk_%' "
                "AND source=? AND created_at > datetime('now', ?) LIMIT 1",
                (source, f"-{hours} hours"),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def portfolio_derisk_pass(broker, t_cfg, source: str, dry_run: bool) -> int:
    """Portfolio-Drawdown Tier 2/3: entrisikt durch anteiliges Trimmen ALLER
    Positionen, wenn das GESAMTdepot stark vom Rolling-Peak faellt.
    Tier 2 (-15%): 25% je Position. Tier 3 (-22%): 50%. 48h-Cooldown gegen
    Verkaufs-Spirale. Reaktiv (keine Vorhersage) — kappt den Gesamt-Drawdown,
    waehrend die Einzel-Stops weiterlaufen."""
    from src.risk.limits import drawdown_tier
    tier, dd = drawdown_tier(source)
    if tier < 2:
        return 0
    if _derisk_cooldown(source, hours=48):
        print(f"  derisk: tier {tier} (dd {dd:.1%}) — Cooldown aktiv, skip")
        return 0
    trim_frac = 0.25 if tier == 2 else 0.50
    sold = 0
    for pos in broker.get_positions():
        if pos.qty <= 0:
            continue
        qty = round(pos.qty * trim_frac, 4)
        if qty <= 0:
            continue
        print(f"  DERISK-T{tier} {pos.ticker}: dd {dd:.1%}, trim {trim_frac:.0%} = {qty} @ {pos.market_price:.2f}")
        if dry_run:
            sold += 1
            continue
        result = _sell_with_limit(broker, pos.ticker, qty, last=pos.market_price)
        _record_trade(
            decision_pred_id=None, ticker=pos.ticker, side="sell", qty=qty,
            eur_value=qty * pos.market_price * eur_per_usd(), price=pos.market_price,
            status=result.status, order_id=result.order_id,
            strategy_label=f"derisk_tier{tier}-v1", source=source,
            notes=f"portfolio drawdown {dd:.1%} tier {tier}, trim {trim_frac:.0%}",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            sold += 1
    if sold and not dry_run:
        try:
            notifier.send_info(
                f"⚠️ Portfolio-Drawdown {dd:.1%} (Tier {tier}) — "
                f"{sold} Positionen um {trim_frac:.0%} getrimmt, Cash erhoeht.",
                label="derisk")
        except Exception:
            pass
    return sold


def buy_pass(broker: BrokerAdapter, cfg, t_cfg: TradingConfig, source: str, dry_run: bool, regime_info: str = "") -> dict:
    """Pruefe alle tradeable Tickers, treffe Decision, fuehre Buys aus."""
    from src.trading import get_active_profile
    from src.trading.decision import latest_risk_score
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

    # Sector-Momentum-Cache fuer Ranking-Bonus
    try:
        _sec_mom_cache = _sector_momentum_map()
    except Exception:
        _sec_mom_cache = {}

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

    # Phase 1: Collect buy-eligible candidates
    eligible = []
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

        # Sizing — ETFs bekommen halbes target (Alpha kommt von Einzelaktien)
        _SECTOR_ETFS = {"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}
        if entry.ticker in _SECTOR_ETFS:
            decision.target_eur = decision.target_eur * 0.5
        # Conviction-Daten fuer dynamisches Sizing
        _pre_momentum = 0.0
        try:
            from src.common.data_loader import get_prices as _gp
            _pp = _gp(entry.ticker, period="1mo")
            if _pp is not None and len(_pp) >= 5:
                _pre_momentum = float(_pp["close"].iloc[-1] / _pp["close"].iloc[0] - 1)
        except Exception:
            pass
        _pre_score = latest_risk_score(entry.ticker)
        decision.extras["momentum_20d"] = _pre_momentum
        decision.extras["risk_composite"] = _pre_score["composite"] if _pre_score else 50.0
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

        score = latest_risk_score(entry.ticker)
        # Momentum: 20-Tage-Rendite als Tiebreaker
        momentum = 0.0
        try:
            from src.common.data_loader import get_prices
            prices = get_prices(entry.ticker, period="1mo")
            if prices is not None and len(prices) >= 5:
                momentum = float(prices["close"].iloc[-1] / prices["close"].iloc[0] - 1)
        except Exception:
            pass
        _sector = _get_sector_for_ticker(t_cfg, entry.ticker) or ""
        _sec_mom = _sec_mom_cache.get(_sector, 0.0)
        eligible.append({
            "ticker": entry.ticker, "decision": decision, "quote": quote, "sz": sz,
            "composite": score["composite"] if score else 0,
            "confidence": score["confidence"] if score else "low",
            "alert": score["alert_level"] if score else 0,
            "triggered": score["triggered_n"] if score else 0,
            "momentum": momentum,
            "sector_momentum": _sec_mom,
        })

    # Rank: Conviction-First — Momentum + Sektor-Momentum + niedriges Risiko
    eligible.sort(key=lambda x: -(x["momentum"] * 100 + x.get("sector_momentum", 0) * 30) + x["composite"])

    # Phase 2: LLM screening
    if eligible and not dry_run:
        try:
            llm_approved = _llm_screen_candidates(eligible, t_cfg, regime_info, held)
            llm_approved_set = set(llm_approved)
        except Exception as e:
            print(f"  llm-screen failed ({e}), all candidates pass")
            llm_approved_set = {item["ticker"] for item in eligible}
    else:
        llm_approved_set = {item["ticker"] for item in eligible}

    # Phase 3: Execute approved buys
    trades_done = []
    for item in eligible:
        ticker = item["ticker"]
        decision, quote, sz = item["decision"], item["quote"], item["sz"]

        if ticker not in llm_approved_set:
            decisions["skips"].append({"ticker": ticker, "reason": "llm-screen: nicht empfohlen"})
            print(f"  SKIP {ticker}: LLM-Screen abgelehnt")
            continue

        if dry_run:
            print(f"  BUY (dry-run) {ticker}: {sz.qty} @ {quote.last:.2f} = {sz.eur_amount:.2f} EUR  ({decision.reason})")
            decisions["buys"].append({"ticker": ticker, "qty": sz.qty, "dry_run": True})
            continue

        if quote.bid > 0 and quote.ask > 0:
            mid = (quote.bid + quote.ask) / 2
            limit_price = round(mid * 1.001, 2)
        elif quote.ask > 0:
            limit_price = round(quote.ask * 1.001, 2)
        else:
            limit_price = round(quote.last * 1.002, 2)
        result = broker.place_order(
            ticker=ticker, side="buy", qty=sz.qty,
            order_type="limit", limit_price=limit_price,
        )
        _record_trade(
            decision_pred_id=decision.decision_pred_id,
            ticker=ticker, side="buy", qty=sz.qty,
            eur_value=sz.eur_amount, price=quote.last,
            status=result.status, order_id=result.order_id,
            strategy_label=f"{t_cfg.mode}-{decision.strategy_label}-v1", source=source,
            notes=decision.reason,
        )
        if result.status == "filled":
            _update_position_after_buy(ticker, f"{t_cfg.mode}-{decision.strategy_label}-v1", source, quote.last)
        print(f"  BUY {ticker}: {sz.qty} @ {quote.last:.2f} = {sz.eur_amount:.2f} EUR  [{result.status}]")
        decisions["buys"].append({"ticker": ticker, "qty": sz.qty, "status": result.status, "order_id": result.order_id})
        trades_done.append({"ticker": ticker, "side": "buy", "qty": sz.qty, "price": quote.last, "reason": decision.reason})

        if result.status in ("filled", "pending_new", "accepted"):
            held.add(ticker)
            open_n += 1
            if open_n >= t_cfg.max_open_positions:
                print(f"  max_open_positions reached, stopping for today")
                break

    return decisions


def _build_candidate_context(item: dict, t_cfg) -> str:
    """Baut reichen Kontext pro Kandidat fuer LLM-Screening."""
    import json as _json
    from src.common.storage import MARKET_DB, LEARNING_DB, connect

    ticker = item["ticker"]
    parts = [f"### {ticker}"]
    parts.append(f"Risk: composite={item['composite']:.1f}, alert={item['alert']}, "
                 f"triggered={item['triggered']}, confidence={item['confidence']}, "
                 f"momentum_20d={item.get('momentum', 0):+.1%}")

    # Fundamentals
    try:
        with connect(MARKET_DB) as conn:
            fund = conn.execute("SELECT * FROM fundamentals WHERE ticker=?", (ticker,)).fetchone()
        if fund:
            parts.append(f"Fundamentals: PE={fund['pe_ratio']}, PB={fund['pb_ratio']}, "
                         f"MCap=${fund['market_cap']/1e9:.0f}B, Beta={fund['beta']}, "
                         f"Sector={fund['sector']}, Div={fund['dividend_yld']}%")
    except Exception:
        pass

    # Key risk dimensions (nur die relevanten)
    try:
        with connect(LEARNING_DB) as conn:
            row = conn.execute(
                "SELECT output_json FROM predictions WHERE job_source='daily_score' AND subject_id=? ORDER BY created_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        if row:
            out = _json.loads(row["output_json"] or "{}")
            active = []
            for d in out.get("dimensions", []):
                s = d.get("score", 0)
                if s >= 15:
                    ev = d.get("evidence", {})
                    detail = ""
                    if d["name"] == "valuation_percentile" and "current_pe" in ev:
                        detail = f" (PE={ev['current_pe']:.1f})"
                    elif d["name"] == "earnings_proximity" and "days_until" in ev:
                        detail = f" ({ev['days_until']}d until earnings)"
                    elif d["name"] == "sentiment_reversal" and "avg_sentiment" in ev:
                        detail = f" (sent={ev['avg_sentiment']:.2f}, neg={ev.get('negative_ratio',0):.0%})"
                    elif d["name"] == "technical_breakdown":
                        detail = f" (RSI={ev.get('rsi','?')}, vs_MA50={ev.get('current',0)/ev.get('ma50',1)-1:+.1%})" if ev.get("ma50") else ""
                    active.append(f"{d['name']}={s:.0f}{detail}")
            if active:
                parts.append(f"Active signals: {', '.join(active)}")
    except Exception:
        pass

    # Sector exposure check
    sector = _get_sector_for_ticker(t_cfg, ticker)
    if sector:
        parts.append(f"Sector: {sector}")

    return "\n".join(parts)


def _llm_screen_candidates(eligible: list, t_cfg, regime_info: str, held: set) -> list:
    """Sonnet-basiertes Pre-Buy Screening mit vollem Kontext."""
    from src.common.llm import call_sonnet, is_configured
    if not is_configured() or not eligible:
        return [item["ticker"] for item in eligible]

    candidates_block = "\n\n".join(_build_candidate_context(item, t_cfg) for item in eligible)
    held_sectors = {}
    for t in held:
        s = _get_sector_for_ticker(t_cfg, t)
        if s:
            held_sectors[s] = held_sectors.get(s, 0) + 1

    system = (
        "Du bist ein erfahrener Portfolio-Manager der Buy-Kandidaten fuer ein autonomes "
        "Paper-Trading-System auf $100k bewertet. Dein Ziel: maximale jaehrliche Rendite bei kontrolliertem Risiko. Bevorzuge Momentum-Aktien in starken Sektoren.\n\n"
        "REGELN:\n"
        "1. APPROVE Kandidaten mit: starkem Momentum (>3% 20d) + niedrigem Composite + Wachstumspotenzial\n"
        "2. REJECT wenn: Earnings in <7 Tagen (Binary-Event-Risiko), negatives Sentiment + "
        "negative Momentum (fallendes Messer), ueberbewertet (PE>80) + negatives Momentum + kein Wachstum\n"
        "3. REJECT wenn Sektor bereits uebergewichtet im Portfolio (>5 Positionen gleicher Sektor)\n"
        "4. Bei Unsicherheit: APPROVE (Opportunity-Cost > Risk fuer Paper-Trading)\n"
        "5. Begruende jede Rejection in 1 Satz\n\n"
        "FORMAT (nur JSON, kein anderer Text):\n"
        '{"approved": ["T1","T2"], "rejected": [{"ticker":"T3","reason":"..."}]}'
    )
    prompt = (
        f"## Markt-Regime: {regime_info}\n"
        f"## Portfolio: {len(held)} Positionen, Sektoren: {held_sectors}\n\n"
        f"## Buy-Kandidaten\n\n{candidates_block}\n\n"
        f"Bewerte jeden Kandidaten."
    )

    result = call_sonnet(
        system=system,
        prompt=prompt,
        job_source="llm_buy_screen",
        subject_type="batch",
        input_summary=f"screen {len(eligible)} candidates, regime={regime_info[:30]}",
        max_tokens=512,
        temperature=0.1,
        estimated_cost_eur=0.02,
    )

    if not result.ok or not result.parsed_json:
        print(f"  llm-screen: fallback (error={result.error})")
        return [item["ticker"] for item in eligible]

    parsed = result.parsed_json
    approved = parsed.get("approved", [])
    rejected = parsed.get("rejected", [])

    if not approved and not rejected:
        return [item["ticker"] for item in eligible]

    for r in rejected:
        if isinstance(r, dict):
            print(f"  llm-screen REJECT {r.get('ticker','?')}: {r.get('reason','?')}")

    if not approved:
        print(f"  llm-screen: all rejected, fallback to all-pass")
        return [item["ticker"] for item in eligible]

    print(f"  llm-screen: {len(approved)} approved, {len(rejected)} rejected, cost {result.cost_eur:.4f}€")
    return approved


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
                print(f"  REGIME-SELL {p.ticker}: Sektor '{sector}' vermieden in {regime.label}")
                if dry_run:
                    sells += 1
                    continue
                result = _sell_with_limit(broker, p.ticker, p.qty, last=p.market_price)
                _record_trade(
                    decision_pred_id=None,
                    ticker=p.ticker, side="sell", qty=p.qty,
                    eur_value=p.market_value_eur, price=p.market_price,
                    status=result.status, order_id=result.order_id,
                    strategy_label="regime_rebalance-v1", source=source,
                    notes=f"regime {transition['from']}->{transition['to']}: avoid {sector}",
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
            result = _sell_with_limit(broker, p.ticker, p.qty, last=p.market_price)
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


WINNER_SCALE_MIN_GAIN = 0.07      # min +7% Gewinn (>5% = echtes Signal, kein Rauschen)
WINNER_SCALE_MAX_PER_RUN = 5
WINNER_SCALE_FACTOR = 0.75        # 75% der aktuellen Position dazukaufen


MAX_PORTFOLIO_LEVERAGE = 1.5  # Gross-Exposure / Equity Obergrenze (Sicherheitsnetz)


def _buy_gate(broker, t_cfg, ticker: str, eur_amount: float):
    """Globales Pre-Buy-Gate fuer ALLE Buy-Pfade (nicht nur buy_pass):
    Kill-Switch, Market-Hours, Trade-Cap, Daily-Loss, Cash-Floor, Sector-Cap,
    Correlation UND portfolioweiter Leverage-Deckel. Gibt (ok, reason) zurueck.
    Fail-CLOSED: bei Datenfehler im Leverage-Check wird blockiert, nicht erlaubt."""
    check = pre_trade_check(broker, t_cfg)
    if not check.ok:
        return False, check.reason
    cash_ok, cash_reason = cash_floor_check(broker, t_cfg)
    if not cash_ok:
        return False, f"cash-floor: {cash_reason}"
    sector_ok, sector_reason = sector_concentration_check(broker, t_cfg, ticker, eur_amount)
    if not sector_ok:
        return False, f"sector-cap: {sector_reason}"
    corr_ok, corr_reason = correlation_check(broker, ticker)
    if not corr_ok:
        return False, f"correlation: {corr_reason}"
    try:
        acc = broker.get_account()
        gross_eur = sum(p.market_value_eur for p in broker.get_positions()) + eur_amount
        lev = gross_eur / acc.equity_eur if acc.equity_eur > 0 else 0
        if lev > MAX_PORTFOLIO_LEVERAGE:
            return False, f"leverage {lev:.2f}x > {MAX_PORTFOLIO_LEVERAGE}x"
    except Exception:
        return False, "leverage: account/positions nicht lesbar"
    return True, "ok"


def winner_scaling_pass(broker, t_cfg, source: str, dry_run: bool) -> int:
    """Stocke Gewinner auf: Positionen mit >10% Gain + positivem Momentum."""
    from src.trading import get_active_profile
    from src.trading.decision import latest_risk_score
    from src.common.data_loader import get_prices

    positions = broker.get_positions()
    fx = eur_per_usd()
    profile = get_active_profile(t_cfg) or {}
    max_pos_eur = profile.get("max_position_eur", t_cfg.max_position_eur)
    account = broker.get_account()
    scaled = 0

    candidates = []
    for pos in positions:
        if pos.avg_price <= 0:
            continue
        gain_pct = (pos.market_price / pos.avg_price) - 1.0
        if gain_pct < WINNER_SCALE_MIN_GAIN:
            continue
        score = latest_risk_score(pos.ticker)
        if not score or score["alert_level"] >= 2:
            continue
        try:
            prices = get_prices(pos.ticker, period="1mo")
            if prices is not None and len(prices) >= 5:
                momentum = float(prices["close"].iloc[-1] / prices["close"].iloc[0] - 1)
            else:
                momentum = 0
        except Exception:
            momentum = 0
        if momentum <= 0:
            continue
        current_eur = pos.market_price * pos.qty * fx
        headroom = max_pos_eur - current_eur
        if headroom < t_cfg.min_position_eur:
            continue
        from src.trading.sizing import conviction_multiplier
        _conv = conviction_multiplier(momentum, score["composite"])
        add_eur = min(current_eur * WINNER_SCALE_FACTOR * _conv, headroom, account.cash_eur * 0.15)
        if add_eur < t_cfg.min_position_eur:
            continue
        candidates.append({
            "pos": pos, "gain": gain_pct, "momentum": momentum,
            "add_eur": add_eur, "risk": score["composite"],
        })

    candidates.sort(key=lambda x: -x["momentum"])

    for cand in candidates[:WINNER_SCALE_MAX_PER_RUN]:
        pos = cand["pos"]
        price_eur = pos.market_price * fx
        qty = round(cand["add_eur"] / price_eur, 4)
        print(f"  WINNER-SCALE {pos.ticker}: +{cand['gain']:.0%} gain, mom={cand['momentum']:+.1%}, "
              f"+{qty} shares ({cand['add_eur']:.0f}€)")
        if dry_run:
            scaled += 1
            continue
        _gate_ok, _gate_reason = _buy_gate(broker, t_cfg, pos.ticker, cand["add_eur"])
        if not _gate_ok:
            print(f"  WINNER-SCALE {pos.ticker} blockiert: {_gate_reason}")
            continue
        quote = broker.get_quote(pos.ticker)
        if quote.bid > 0 and quote.ask > 0:
            mid = (quote.bid + quote.ask) / 2
            limit_price = round(mid * 1.001, 2)
        elif quote.ask > 0:
            limit_price = round(quote.ask * 1.001, 2)
        else:
            limit_price = round(quote.last * 1.002, 2)
        result = broker.place_order(ticker=pos.ticker, side="buy", qty=qty,
                                    order_type="limit", limit_price=limit_price)
        _record_trade(
            decision_pred_id=None,
            ticker=pos.ticker, side="buy", qty=qty,
            eur_value=cand["add_eur"], price=pos.market_price,
            status=result.status, order_id=result.order_id,
            strategy_label="winner_scale-v1", source=source,
            notes=f"winner scaling: +{cand['gain']:.0%}, momentum={cand['momentum']:+.1%}",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            scaled += 1
    return scaled


def atr_stop_update(broker, t_cfg, source: str) -> int:
    """Update stop_loss_price basierend auf 2x ATR statt fixem Prozentsatz."""
    from src.common.data_loader import get_prices
    import numpy as np

    updated = 0
    positions = broker.get_positions()
    with connect(TRADING_DB) as conn:
        for pos in positions:
            try:
                prices = get_prices(pos.ticker, period="3mo")
                if prices is None or len(prices) < 20:
                    continue
                high = prices["high"].values[-20:]
                low = prices["low"].values[-20:]
                close = prices["close"].values[-21:-1]
                tr = np.maximum(high - low, np.maximum(
                    np.abs(high - close), np.abs(low - close)))
                atr = float(np.mean(tr))
                atr_stop = round(pos.market_price - 2.0 * atr, 2)
                if atr_stop <= 0:
                    continue
                row = conn.execute(
                    "SELECT stop_loss_price FROM positions WHERE ticker=? AND source=?",
                    (pos.ticker, source),
                ).fetchone()
                if not row:
                    continue
                old_stop = row["stop_loss_price"] or 0
                if atr_stop > old_stop:
                    conn.execute(
                        "UPDATE positions SET stop_loss_price=?, last_updated=datetime('now') WHERE ticker=? AND source=?",
                        (atr_stop, pos.ticker, source),
                    )
                    updated += 1
            except Exception:
                continue
    return updated


DIP_BUY_MIN_DROP = -0.025
DIP_BUY_MAX_RISK = 40
DIP_BUY_MAX_PER_RUN = 4
DIP_BUY_SIZING_FACTOR = 0.9


def dip_buying_pass(broker, cfg, t_cfg, source: str, dry_run: bool) -> int:
    """Kauft Qualitaetsaktien die heute >3% gefallen sind (Mean-Reversion)."""
    from src.trading.decision import latest_risk_score
    from src.trading import get_active_profile
    from src.common.data_loader import get_prices

    held = {p.ticker for p in broker.get_positions()}
    fx = eur_per_usd()
    profile = get_active_profile(t_cfg) or {}
    target_eur = profile.get("max_position_eur", t_cfg.max_position_eur)
    account = broker.get_account()
    bought = 0

    candidates = []
    for entry in cfg.universe:
        if entry.ring not in t_cfg.tradeable_rings or entry.ticker in held:
            continue
        try:
            prices = get_prices(entry.ticker, period="5d")
            if prices is None or len(prices) < 2:
                continue
            today_ret = float(prices["close"].iloc[-1] / prices["close"].iloc[-2] - 1)
            if today_ret > DIP_BUY_MIN_DROP:
                continue
            week_ret = float(prices["close"].iloc[-1] / prices["close"].iloc[0] - 1)
            if week_ret < -0.10:
                continue
        except Exception:
            continue
        score = latest_risk_score(entry.ticker)
        if not score or score["composite"] > DIP_BUY_MAX_RISK or score["alert_level"] >= 2:
            continue
        candidates.append({"entry": entry, "drop": today_ret, "risk": score["composite"]})

    candidates.sort(key=lambda x: x["drop"])
    for cand in candidates[:DIP_BUY_MAX_PER_RUN]:
        entry = cand["entry"]
        eur_amount = min(target_eur * DIP_BUY_SIZING_FACTOR, account.cash_eur * 0.12)
        if eur_amount < t_cfg.min_position_eur:
            continue
        quote = broker.get_quote(entry.ticker)
        price_eur = quote.last * fx
        if price_eur <= 0:
            continue
        qty = round(eur_amount / price_eur, 4)
        print(f"  DIP-BUY {entry.ticker}: {cand['drop']:+.1%} today, risk={cand['risk']:.0f}, "
              f"{qty} @ ${quote.last:.2f} = {eur_amount:.0f}EUR")
        if dry_run:
            bought += 1
            continue
        _gate_ok, _gate_reason = _buy_gate(broker, t_cfg, entry.ticker, eur_amount)
        if not _gate_ok:
            print(f"  DIP-BUY {entry.ticker} blockiert: {_gate_reason}")
            continue
        if quote.bid > 0 and quote.ask > 0:
            limit_price = round((quote.bid + quote.ask) / 2 * 1.001, 2)
        else:
            limit_price = round(quote.last * 1.002, 2)
        result = broker.place_order(ticker=entry.ticker, side="buy", qty=qty,
                                    order_type="limit", limit_price=limit_price)
        _record_trade(
            decision_pred_id=None, ticker=entry.ticker, side="buy", qty=qty,
            eur_value=eur_amount, price=quote.last,
            status=result.status, order_id=result.order_id,
            strategy_label="dip_buy-v1", source=source,
            notes=f"dip buy: {cand['drop']:+.1%} today, risk={cand['risk']:.0f}",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            bought += 1
            try:
                notifier.send_trade(ticker=entry.ticker, side="buy", qty=qty,
                    eur=eur_amount, price_usd=quote.last,
                    reason=f"DIP-BUY: {cand['drop']:+.1%} drop, risk={cand['risk']:.0f}",
                    paper=broker.is_paper)
            except Exception:
                pass
    return bought




RING3_LIQUIDATION_MAX_PER_RUN = 5

def ring3_liquidation_pass(broker, cfg, t_cfg, source: str, dry_run: bool) -> int:
    """Verkauft Positionen die nicht mehr in tradeable_rings sind."""
    positions = broker.get_positions()
    fx = eur_per_usd()
    tradeable_tickers = {e.ticker for e in cfg.universe if e.ring in t_cfg.tradeable_rings}
    
    non_tradeable = []
    for pos in positions:
        if pos.ticker in tradeable_tickers or pos.qty <= 0:
            continue
        pnl_pct = (pos.market_price / pos.avg_price - 1.0) if pos.avg_price > 0 else 0
        non_tradeable.append({"pos": pos, "pnl": pnl_pct})
    
    non_tradeable.sort(key=lambda x: x["pnl"])
    sold = 0
    
    for item in non_tradeable[:RING3_LIQUIDATION_MAX_PER_RUN]:
        pos = item["pos"]
        print(f"  RING3-EXIT {pos.ticker}: PnL={item['pnl']:+.1%}, "
              f"value={pos.market_value_eur:.0f}EUR — not in tradeable_rings")
        if dry_run:
            sold += 1
            continue
        result = _sell_with_limit(broker, pos.ticker, pos.qty, last=pos.market_price)
        _record_trade(
            decision_pred_id=None,
            ticker=pos.ticker, side="sell", qty=pos.qty,
            eur_value=pos.market_value_eur, price=pos.market_price,
            status=result.status, order_id=result.order_id,
            strategy_label="ring3_exit-v1", source=source,
            notes=f"ring3 liquidation: not in tradeable_rings",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            sold += 1
            try:
                notifier.send_trade(
                    ticker=pos.ticker, side="sell", qty=pos.qty,
                    eur=pos.market_value_eur, price_usd=pos.market_price,
                    reason=f"Ring3-Exit: PnL={item['pnl']:+.1%}",
                    paper=broker.is_paper,
                )
            except Exception:
                pass
    return sold

MOMENTUM_EXIT_MIN_HOLD_DAYS = 14
MOMENTUM_EXIT_MAX_PER_RUN = 4
_SECTOR_ETFS = {"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}


def momentum_exit_pass(broker, t_cfg, source: str, dry_run: bool) -> int:
    """Verkauft Positionen mit negativem Momentum seit 2+ Wochen — totes Kapital befreien."""
    from src.common.data_loader import get_prices
    from src.trading.decision import latest_risk_score

    positions = broker.get_positions()
    fx = eur_per_usd()
    candidates = []

    for pos in positions:
        if pos.avg_price <= 0:
            continue
        # ETFs bevorzugt rauswerfen
        is_etf = pos.ticker in _SECTOR_ETFS
        try:
            prices = get_prices(pos.ticker, period="1mo")
            if prices is None or len(prices) < 10:
                continue
            ret_20d = float(prices["close"].iloc[-1] / prices["close"].iloc[0] - 1)
            ret_10d = float(prices["close"].iloc[-1] / prices["close"].iloc[-min(10, len(prices)):].iloc[0] - 1)
        except Exception:
            continue
        # Stocks: nur verkaufen wenn BEIDE Zeitrahmen negativ
        # ETFs: aggressiver — Kapital in Stocks umleiten
        if is_etf:
            if ret_20d >= 0.02:
                continue
        else:
            if ret_20d >= 0 or ret_10d >= -0.01:
                continue
        # Nicht verkaufen wenn Risk-Score niedrig (Position ist fundamental ok, nur Momentum schwach)
        score = latest_risk_score(pos.ticker)
        if score and score["composite"] < 20 and not is_etf:
            continue
        pnl_pct = (pos.market_price / pos.avg_price) - 1.0
        candidates.append({
            "pos": pos, "ret_20d": ret_20d, "ret_10d": ret_10d,
            "pnl": pnl_pct, "is_etf": is_etf,
            "composite": score["composite"] if score else 50,
        })

    # ETFs zuerst, dann nach schlechtestem Momentum
    candidates.sort(key=lambda x: (not x["is_etf"], x["ret_20d"]))
    sold = 0

    for cand in candidates[:MOMENTUM_EXIT_MAX_PER_RUN]:
        pos = cand["pos"]
        print(f"  MOMENTUM-EXIT {pos.ticker}: 20d={cand['ret_20d']:+.1%}, "
              f"10d={cand['ret_10d']:+.1%}, PnL={cand['pnl']:+.1%}, "
              f"{'ETF' if cand['is_etf'] else 'Stock'}")
        if dry_run:
            sold += 1
            continue
        result = _sell_with_limit(broker, pos.ticker, pos.qty, last=pos.market_price)
        _record_trade(
            decision_pred_id=None,
            ticker=pos.ticker, side="sell", qty=pos.qty,
            eur_value=pos.market_value_eur, price=pos.market_price,
            status=result.status, order_id=result.order_id,
            strategy_label="momentum_exit-v1", source=source,
            notes=f"dead momentum: 20d={cand['ret_20d']:+.1%}, 10d={cand['ret_10d']:+.1%}",
        )
        if result.status in ("filled", "pending_new", "accepted"):
            sold += 1
            try:
                notifier.send_trade(
                    ticker=pos.ticker, side="sell", qty=pos.qty,
                    eur=pos.market_value_eur, price_usd=pos.market_price,
                    reason=f"Momentum-Exit: 20d={cand['ret_20d']:+.1%}",
                    paper=broker.is_paper,
                )
            except Exception:
                pass
    return sold


def _cancel_stale_orders(broker, source: str, max_age_hours: int = 4) -> int:
    """Cancelt unfilled Orders die aelter als max_age_hours sind."""
    cancelled = 0
    with connect(TRADING_DB) as conn:
        stale = conn.execute(
            """SELECT broker_order_id, ticker, side, qty, price, strategy_label
               FROM trades
               WHERE source=? AND status IN ('accepted','pending_new','new')
                 AND created_at < datetime('now', ?)""",
            (source, f"-{max_age_hours} hours"),
        ).fetchall()
    for order in stale:
        oid = order["broker_order_id"]
        if not oid:
            continue
        try:
            broker.cancel_order(oid)
            with connect(TRADING_DB) as conn:
                conn.execute(
                    "UPDATE trades SET status='canceled', notes=notes||' [stale-cancel]' WHERE broker_order_id=?",
                    (oid,),
                )
            print(f"  CANCEL stale {order['side']} {order['ticker']} (order {oid[:8]}..)")
            cancelled += 1
        except Exception:
            pass
    return cancelled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Decisions berechnen und printen, keine Orders.")
    parser.add_argument("--mock", action="store_true",
                        help="Mock-Broker statt config.yaml-broker.")
    parser.add_argument("--skip-stop-loss", action="store_true")
    parser.add_argument("--skip-buys", action="store_true")
    args = parser.parse_args()

    lock_path = Path("/tmp/invest-pi-strategy.lock")
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another run_strategy.py is already running — exiting.")
        lock_fp.close()
        return
    lock_fp.write(str(dt.datetime.now()))
    lock_fp.flush()

    try:
        _run_strategy_locked(args)
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


def _run_strategy_locked(args):
    init_all()

    try:
        from src.learning.weight_optimizer import load_latest_weights, apply_weights
        saved = load_latest_weights()
        if saved:
            apply_weights(saved)
    except Exception:
        pass

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

    # Cancel stale orders (>4h old, not filled)
    try:
        n_cancelled = _cancel_stale_orders(broker, src, max_age_hours=4)
        if n_cancelled:
            print(f"  stale-orders cancelled: {n_cancelled}")
    except Exception as e:
        print(f"  stale-order cleanup skipped: {e}")

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
        n_dr = portfolio_derisk_pass(broker, t_cfg, src, args.dry_run)
        if n_dr:
            print(f"  portfolio-derisk pass: {n_dr} trims")

    # Risk-Score-basierte Sells: RED-Alert Positionen verkaufen
    if not args.skip_stop_loss:
        n_risk = risk_sell_pass(broker, t_cfg, src, args.dry_run)
        if n_risk:
            print(f"  risk-sell pass: {n_risk} sells")

    # Momentum-Exit: totes Kapital befreien
    if not args.skip_stop_loss:
        try:
            n_mom = momentum_exit_pass(broker, t_cfg, src, args.dry_run)
            if n_mom:
                print(f"  momentum-exit: {n_mom} sells")
        except Exception as e:
            print(f"  momentum-exit skipped: {e}")

    # Ring-3-Liquidation: Positionen verkaufen die nicht mehr tradeable sind
    if not args.skip_stop_loss:
        try:
            n_r3 = ring3_liquidation_pass(broker, cfg, t_cfg, src, args.dry_run)
            if n_r3:
                print(f"  ring3-liquidation: {n_r3} sells")
        except Exception as e:
            print(f"  ring3-liquidation skipped: {e}")

    # Regime-Rebalancing bei Wechsel
    if transition:
        n_rb = regime_rebalance_pass(broker, t_cfg, regime, transition, src, args.dry_run)
        if n_rb:
            print(f"  regime-rebalance: {n_rb} sells")

    regime_str = f"{regime.label} ({regime.probability:.0%})" if regime else "unbekannt"

    if not args.skip_buys:
        decisions = buy_pass(broker, cfg, t_cfg, src, args.dry_run, regime_info=regime_str)
        print(f"\n  buys:  {len(decisions['buys'])}")
        print(f"  skips: {len(decisions['skips'])}")
        if decisions['errors']:
            print(f"  errors: {len(decisions['errors'])}")

        # Top-Up nur 1x pro Tag (nicht bei jedem Strategy-Run)
        try:
            from src.common.storage import TRADING_DB, connect as _tc
            with _tc(TRADING_DB) as _conn:
                _today_topups = _conn.execute(
                    "SELECT count(*) as n FROM trades WHERE strategy_label='topup-v1' AND date(created_at)=date('now')"
                ).fetchone()["n"]
            if _today_topups == 0:
                from scripts.weekly_rotation import topup_pass
                from src.trading import get_active_profile
                profile = get_active_profile(t_cfg)
                topups = topup_pass(broker, t_cfg, profile, src, args.dry_run)
                if topups:
                    print(f"  top-ups: {len(topups)}")
        except Exception as e:
            print(f"  top-up skipped: {e}")

    # Winner Scaling: Gewinner mit positivem Momentum aufstocken
    if not args.skip_buys:
        try:
            n_scaled = winner_scaling_pass(broker, t_cfg, src, args.dry_run)
            if n_scaled:
                print(f"  winner-scaling: {n_scaled} positions scaled up")
        except Exception as e:
            print(f"  winner-scaling skipped: {e}")

    # Dip-Buying: Qualitaetsaktien bei Tages-Dips kaufen
    if not args.skip_buys:
        try:
            n_dips = dip_buying_pass(broker, cfg, t_cfg, src, args.dry_run)
            if n_dips:
                print(f"  dip-buys: {n_dips}")
        except Exception as e:
            print(f"  dip-buying skipped: {e}")

    # ATR-basierte Stop-Loss-Updates
    try:
        n_atr = atr_stop_update(broker, t_cfg, src)
        if n_atr:
            print(f"  atr-stops updated: {n_atr}")
    except Exception as e:
        print(f"  atr-stop update skipped: {e}")

    _take_equity_snapshot(broker, src, notes="run_strategy:end")
    final = broker.get_account()
    print(f"\n  Final equity: {final.equity_eur:.2f} EUR  (cash {final.cash_eur:.2f})")

    # Post-Trade Turbo-Learning
    try:
        _post_trade_learning(broker, src)
    except Exception as e:
        print(f"  post-trade-learning skipped: {e}")


def _post_trade_learning(broker, source: str) -> None:
    """
    Sofortiges Feedback nach jedem Strategy-Run:
    - Welche Dimensionen haben die heutigen Trades getriggert?
    - Wie performen die Positionen seit Kauf?
    - Equity-Delta seit letztem Run?
    Speichert Insights in learning DB fuer den Weight-Optimizer.
    """
    import json
    from src.common.storage import TRADING_DB, LEARNING_DB, connect

    with connect(TRADING_DB) as conn:
        today_trades = conn.execute(
            """SELECT ticker, side, strategy_label, notes, price, qty, eur_value
               FROM trades WHERE source=? AND date(created_at)=date('now')
               ORDER BY created_at DESC""",
            (source,),
        ).fetchall()

    if not today_trades:
        return

    # Sammle Performance-Daten fuer gehaltene Positionen
    positions = broker.get_positions()
    perf_data = {}
    for p in positions:
        if p.avg_price > 0:
            perf_data[p.ticker] = {
                "pnl_pct": round((p.market_price / p.avg_price - 1) * 100, 2),
                "value_eur": round(p.market_value_eur, 2),
            }

    # Equity-Delta
    with connect(TRADING_DB) as conn:
        snaps = conn.execute(
            """SELECT total_usd, timestamp FROM equity_snapshots
               WHERE source=? ORDER BY timestamp DESC LIMIT 2""",
            (source,),
        ).fetchall()
    equity_delta = None
    if len(snaps) == 2 and snaps[0]["total_usd"] and snaps[1]["total_usd"]:
        equity_delta = round(snaps[0]["total_usd"] - snaps[1]["total_usd"], 2)

    # Speichere aggregierte Insights
    insight = {
        "trades_today": len(today_trades),
        "buys": sum(1 for t in today_trades if t["side"] == "buy"),
        "sells": sum(1 for t in today_trades if t["side"] == "sell"),
        "equity_delta_usd": equity_delta,
        "positions_performance": perf_data,
        "strategies_used": list(set(t["strategy_label"] for t in today_trades if t["strategy_label"])),
    }

    with connect(LEARNING_DB) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO kv_store (key, value, updated_at)
               VALUES ('post_trade_insight_latest', ?, datetime('now'))""",
            (json.dumps(insight),),
        )

    winners = sum(1 for v in perf_data.values() if v["pnl_pct"] > 0)
    losers = sum(1 for v in perf_data.values() if v["pnl_pct"] < 0)
    delta_str = f", equity Δ${equity_delta:+.0f}" if equity_delta else ""
    print(f"  post-trade: {len(today_trades)} trades, {winners}W/{losers}L positions{delta_str}")


if __name__ == "__main__":
    main()

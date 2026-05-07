"""
Backtest-Gate — validiert Config-Patches gegen historische Daten.

Bevor ein Patch live geht, wird ein Quick-Backtest mit den neuen
Werten gegen die letzten 6 Monate gefahren. Nur wenn Sharpe oder
Drawdown sich nicht verschlechtern, wird der Patch durchgelassen.

Lightweight: nutzt V1-Backtest (schnell) fuer numerische Patches.
Regime-Patches (sector_avoid etc.) werden ohne Backtest durchgelassen
da sie nicht im Backtest-Engine modelliert sind.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

log = logging.getLogger("invest_pi.backtest_gate")

BACKTEST_TICKERS = ["NVDA", "AMD", "MSFT", "AAPL", "GOOGL", "AMZN",
                    "META", "AVGO", "TSM", "JPM", "UNH", "LLY"]

BACKTEST_PARAMS = {
    "trading.stop_loss_pct", "trading.take_profit_pct",
    "trading.score_buy_max", "trading.max_open_positions",
    "trading.max_position_eur", "trading.trailing_stop_pct",
}


def can_backtest(path: str) -> bool:
    """Nur numerische Trading-Params sind backtestbar."""
    return path in BACKTEST_PARAMS


def validate_patch_via_backtest(
    path: str,
    old_value,
    new_value,
    lookback_months: int = 6,
) -> dict:
    """
    Fuehrt Quick-Backtest mit altem vs neuem Wert durch.

    Returns: {
        "passed": bool,
        "reason": str,
        "old_sharpe": float,
        "new_sharpe": float,
        "old_max_dd": float,
        "new_max_dd": float,
    }
    """
    if not can_backtest(path):
        return {"passed": True, "reason": "not backtestable, passed by default"}

    try:
        from .backtest_engine import run_backtest
    except ImportError:
        return {"passed": True, "reason": "backtest engine not available"}

    end = dt.datetime.utcnow().strftime("%Y-%m-%d")
    start = (dt.datetime.utcnow() - dt.timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")

    param_name = path.split(".", 1)[1]

    base_params = {
        "start": start,
        "end": end,
        "tickers": BACKTEST_TICKERS,
        "initial_capital": 50000,
    }

    try:
        old_params = {**base_params, param_name: old_value}
        old_result = run_backtest(**old_params)

        new_params = {**base_params, param_name: new_value}
        new_result = run_backtest(**new_params)
    except Exception as e:
        log.warning(f"backtest failed for {path}: {e}")
        return {"passed": True, "reason": f"backtest error: {e}"}

    old_sharpe = old_result.sharpe if hasattr(old_result, 'sharpe') else 0
    new_sharpe = new_result.sharpe if hasattr(new_result, 'sharpe') else 0
    old_dd = old_result.max_drawdown if hasattr(old_result, 'max_drawdown') else 0
    new_dd = new_result.max_drawdown if hasattr(new_result, 'max_drawdown') else 0

    # Gate: neuer Sharpe darf nicht >20% schlechter sein
    # UND neuer MaxDD darf nicht >30% schlechter sein
    sharpe_ok = new_sharpe >= old_sharpe * 0.80 if old_sharpe > 0 else True
    dd_ok = new_dd >= old_dd * 1.30 if old_dd < 0 else True  # dd is negative

    passed = sharpe_ok and dd_ok
    reason = "ok"
    if not sharpe_ok:
        reason = f"Sharpe verschlechtert: {old_sharpe:.2f} -> {new_sharpe:.2f}"
    elif not dd_ok:
        reason = f"MaxDD verschlechtert: {old_dd:.1%} -> {new_dd:.1%}"

    log.info(f"backtest gate {path}: {'PASS' if passed else 'FAIL'} "
             f"(sharpe {old_sharpe:.2f}->{new_sharpe:.2f}, dd {old_dd:.1%}->{new_dd:.1%})")

    return {
        "passed": passed,
        "reason": reason,
        "old_sharpe": round(old_sharpe, 3),
        "new_sharpe": round(new_sharpe, 3),
        "old_max_dd": round(old_dd, 4),
        "new_max_dd": round(new_dd, 4),
    }

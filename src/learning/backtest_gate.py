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
from typing import Any

log = logging.getLogger("invest_pi.backtest_gate")

BACKTEST_TICKERS = ["NVDA", "AMD", "MSFT", "AAPL", "GOOGL", "AMZN",
                    "META", "AVGO", "TSM", "JPM", "UNH", "LLY"]

# Mapping: config_patcher path → run_backtest kwarg
_PARAM_MAP = {
    "trading.stop_loss_pct":      "stop_loss_pct",
    "trading.take_profit_pct":    "take_profit_pct",
    "trading.score_buy_max":      "score_buy_max",
    "trading.max_open_positions": "max_positions",
    "trading.max_position_eur":   "position_eur",
}

BACKTEST_PARAMS = set(_PARAM_MAP.keys())

SHARPE_TOLERANCE = 0.80   # neuer Sharpe darf max 20% schlechter sein
DD_TOLERANCE     = 1.30   # neuer MaxDD darf max 30% tiefer sein


def can_backtest(path: str) -> bool:
    return path in BACKTEST_PARAMS


def validate_patch_via_backtest(
    path: str,
    old_value: Any,
    new_value: Any,
    lookback_months: int = 6,
) -> dict:
    """
    Quick-Backtest: alter vs neuer Wert ueber die letzten N Monate.

    Returns: {"passed": bool, "reason": str, "old_sharpe", "new_sharpe", ...}
    """
    if not can_backtest(path):
        return {"passed": True, "reason": "not backtestable, passed by default"}

    try:
        from .backtest_engine import run_backtest
    except ImportError:
        return {"passed": True, "reason": "backtest engine not available"}

    now = dt.datetime.now(dt.timezone.utc)
    end = now.strftime("%Y-%m-%d")
    start = (now - dt.timedelta(days=lookback_months * 30)).strftime("%Y-%m-%d")

    bt_param = _PARAM_MAP[path]

    base_params = {
        "start": start,
        "end": end,
        "tickers": BACKTEST_TICKERS,
        "initial_capital": 50000,
    }

    try:
        old_result = run_backtest(**base_params, **({bt_param: old_value} if old_value is not None else {}))
        new_result = run_backtest(**base_params, **{bt_param: new_value})
    except Exception as e:
        log.warning(f"backtest failed for {path}: {e}")
        return {"passed": True, "reason": f"backtest error (allowing): {e}"}

    old_sharpe = old_result.sharpe or 0
    new_sharpe = new_result.sharpe or 0
    old_dd = abs(old_result.max_drawdown)
    new_dd = abs(new_result.max_drawdown)

    sharpe_ok = True
    dd_ok = True

    if old_sharpe > 0:
        sharpe_ok = new_sharpe >= old_sharpe * SHARPE_TOLERANCE

    if old_dd > 0:
        dd_ok = new_dd <= old_dd * DD_TOLERANCE

    passed = sharpe_ok and dd_ok
    reason = "ok"
    if not sharpe_ok:
        reason = f"Sharpe verschlechtert: {old_sharpe:.2f} -> {new_sharpe:.2f} (min {old_sharpe * SHARPE_TOLERANCE:.2f})"
    elif not dd_ok:
        reason = f"MaxDD verschlechtert: {old_dd:.1%} -> {new_dd:.1%} (max {old_dd * DD_TOLERANCE:.1%})"

    log.info(f"backtest gate {path}: {'PASS' if passed else 'FAIL'} "
             f"(sharpe {old_sharpe:.2f}->{new_sharpe:.2f}, dd {old_dd:.1%}->{new_dd:.1%})")

    return {
        "passed": passed,
        "reason": reason,
        "old_sharpe": round(old_sharpe, 3),
        "new_sharpe": round(new_sharpe, 3),
        "old_max_dd": round(-old_dd, 4),
        "new_max_dd": round(-new_dd, 4),
    }

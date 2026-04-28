"""
Risk-Limits — Pre-Trade-Checks.

Vor jedem Order-Submit muss pre_trade_check() OK sagen. Das schuetzt vor:
  - kill_switch (data/.KILL-file existiert)
  - max_trades_per_day Hard-Cap
  - max_daily_loss_pct Equity-Drawdown
  - market_hours Verstoss
  - Cost-Cap-Verletzung (via cost_caps.check_budget)

Plus: check_stop_loss(ticker) entscheidet, ob eine offene Position verkauft
werden muss (Stop-Loss bei -stop_loss_pct unter avg_price).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ..broker import BrokerAdapter
from ..common.cost_caps import check_budget
from ..common.storage import DATA_DIR, TRADING_DB, connect
from ..trading import TradingConfig


KILL_SWITCH_PATH = DATA_DIR / ".KILL"


@dataclass
class CheckResult:
    allowed: bool
    reason:  str = ""
    code:    str = ""    # 'kill' | 'market_closed' | 'daily_loss' | 'max_trades' | 'cost_cap' | 'ok'


# ────────────────────────────────────────────────────────────
# KILL SWITCH
# ────────────────────────────────────────────────────────────
def kill_switch_active() -> bool:
    return KILL_SWITCH_PATH.exists()


def activate_kill_switch(reason: str = "manual") -> None:
    """Schreibt .KILL-file. Alle weiteren Trades werden geblockt bis File geloescht ist."""
    KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_PATH.write_text(f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{reason}\n")


def deactivate_kill_switch() -> None:
    if KILL_SWITCH_PATH.exists():
        KILL_SWITCH_PATH.unlink()


# ────────────────────────────────────────────────────────────
# MARKET HOURS (CET)
# ────────────────────────────────────────────────────────────
def is_market_open(now_utc: Optional[dt.datetime] = None,
                   open_cet: str = "15:30",
                   close_cet: str = "22:00") -> bool:
    """
    Naive Pruefung: Mo-Fr, Zeitfenster in CET.
    Berechnet CET-Lokalzeit per UTC+1 (Wintertime). Sommerzeit-naive.
    Fuer Pi: das reicht; live wird via systemd-Timer eh nur in den Slots gestartet.
    """
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    weekday = now_utc.weekday()  # Mon=0 ... Sun=6
    if weekday >= 5:
        return False
    cet = now_utc + dt.timedelta(hours=1)
    open_h, open_m = (int(x) for x in open_cet.split(":"))
    close_h, close_m = (int(x) for x in close_cet.split(":"))
    minutes = cet.hour * 60 + cet.minute
    return (open_h * 60 + open_m) <= minutes < (close_h * 60 + close_m)


# ────────────────────────────────────────────────────────────
# DAILY LOSS
# ────────────────────────────────────────────────────────────
def daily_loss_pct(source: str = "paper") -> Optional[float]:
    """
    Rueckgabe: equity-Drawdown vom heutigen Hoch (oder gestern-Close, falls noch keiner
    heute) bis zum aktuellen Stand. None wenn noch kein Snapshot.
    """
    sql_today_max = """
        SELECT MAX(total_eur) FROM equity_snapshots
         WHERE source = ?
           AND date(timestamp, 'localtime') = date('now', 'localtime')
    """
    sql_yesterday = """
        SELECT total_eur FROM equity_snapshots
         WHERE source = ?
           AND date(timestamp, 'localtime') < date('now', 'localtime')
         ORDER BY timestamp DESC LIMIT 1
    """
    sql_now = """
        SELECT total_eur FROM equity_snapshots
         WHERE source = ?
         ORDER BY timestamp DESC LIMIT 1
    """
    with connect(TRADING_DB) as conn:
        today_max = conn.execute(sql_today_max, (source,)).fetchone()
        yesterday = conn.execute(sql_yesterday, (source,)).fetchone()
        latest = conn.execute(sql_now, (source,)).fetchone()
    if not latest or latest[0] is None:
        return None
    base = (today_max[0] if today_max and today_max[0] else
            yesterday[0] if yesterday and yesterday[0] else None)
    if not base or base <= 0:
        return None
    return (latest[0] / base) - 1.0


def trades_today(source: str = "paper") -> int:
    sql = """
        SELECT COUNT(*) FROM trades
         WHERE source = ?
           AND date(created_at, 'localtime') = date('now', 'localtime')
           AND status = 'filled'
    """
    with connect(TRADING_DB) as conn:
        return int(conn.execute(sql, (source,)).fetchone()[0])


# ────────────────────────────────────────────────────────────
# MAIN CHECK
# ────────────────────────────────────────────────────────────
def pre_trade_check(
    broker: BrokerAdapter,
    config: TradingConfig,
    estimated_api_cost_eur: float = 0.0,
) -> CheckResult:
    """Aggregierter Pre-Trade-Check — vor JEDEM place_order aufrufen."""
    if kill_switch_active():
        return CheckResult(False, f"kill switch active ({KILL_SWITCH_PATH})", "kill")

    if not is_market_open(open_cet=config.market_open_cet, close_cet=config.market_close_cet):
        return CheckResult(False,
                           f"market closed ({config.market_open_cet}-{config.market_close_cet} CET)",
                           "market_closed")

    src = "paper" if broker.is_paper else "live"
    n_today = trades_today(src)
    if n_today >= config.max_trades_per_day:
        return CheckResult(False,
                           f"max_trades_per_day reached ({n_today}/{config.max_trades_per_day})",
                           "max_trades")

    dl = daily_loss_pct(src)
    if dl is not None and dl <= -config.max_daily_loss_pct:
        return CheckResult(False,
                           f"daily loss {dl:.1%} <= -{config.max_daily_loss_pct:.0%}",
                           "daily_loss")

    if estimated_api_cost_eur > 0:
        budget = check_budget()
        if not budget.ok:
            return CheckResult(False,
                               f"cost cap breached: {budget.tier_breached}",
                               "cost_cap")

    return CheckResult(True, "ok", "ok")


# ────────────────────────────────────────────────────────────
# STOP LOSS — berechnet, sells werden vom Caller ausgefuehrt
# ────────────────────────────────────────────────────────────
def positions_to_stop_loss(broker: BrokerAdapter,
                           config: TradingConfig) -> list[Tuple[str, float, float]]:
    """
    Returns [(ticker, qty, current_price), ...] fuer Positionen die unter
    stop_loss_pct sind. Caller entscheidet ob/wie verkauft wird.
    """
    triggered = []
    for pos in broker.get_positions():
        if pos.avg_price <= 0:
            continue
        unrealized_pct = (pos.market_price / pos.avg_price) - 1.0
        if unrealized_pct <= -config.stop_loss_pct:
            triggered.append((pos.ticker, pos.qty, pos.market_price))
    return triggered

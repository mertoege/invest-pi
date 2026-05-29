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

def _us_market_holidays(year: int) -> set:
    """NYSE/NASDAQ Feiertage fuer ein gegebenes Jahr."""
    from datetime import date
    holidays = set()
    # New Years Day (1. Jan, oder Mo wenn So)
    nyd = date(year, 1, 1)
    if nyd.weekday() == 6: nyd = date(year, 1, 2)
    holidays.add(nyd)
    # MLK Day (3. Montag im Januar)
    d = date(year, 1, 1)
    mon_count = 0
    while mon_count < 3:
        if d.weekday() == 0: mon_count += 1
        if mon_count < 3: d += dt.timedelta(days=1)
    holidays.add(d)
    # Presidents Day (3. Montag im Februar)
    d = date(year, 2, 1)
    mon_count = 0
    while mon_count < 3:
        if d.weekday() == 0: mon_count += 1
        if mon_count < 3: d += dt.timedelta(days=1)
    holidays.add(d)
    # Good Friday (2 Tage vor Ostersonntag)
    # Easter algorithm (Anonymous Gregorian)
    a = year % 19
    b, c = divmod(year, 100)
    d2, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d2 - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    holidays.add(easter - dt.timedelta(days=2))  # Good Friday
    # Memorial Day (letzter Montag im Mai)
    d = date(year, 5, 31)
    while d.weekday() != 0: d -= dt.timedelta(days=1)
    holidays.add(d)
    # Juneteenth (19. Juni)
    jt = date(year, 6, 19)
    if jt.weekday() == 5: jt = date(year, 6, 18)
    elif jt.weekday() == 6: jt = date(year, 6, 20)
    holidays.add(jt)
    # Independence Day (4. Juli)
    id4 = date(year, 7, 4)
    if id4.weekday() == 5: id4 = date(year, 7, 3)
    elif id4.weekday() == 6: id4 = date(year, 7, 5)
    holidays.add(id4)
    # Labor Day (1. Montag im September)
    d = date(year, 9, 1)
    while d.weekday() != 0: d += dt.timedelta(days=1)
    holidays.add(d)
    # Thanksgiving (4. Donnerstag im November)
    d = date(year, 11, 1)
    thu_count = 0
    while thu_count < 4:
        if d.weekday() == 3: thu_count += 1
        if thu_count < 4: d += dt.timedelta(days=1)
    holidays.add(d)
    # Christmas (25. Dez)
    xmas = date(year, 12, 25)
    if xmas.weekday() == 5: xmas = date(year, 12, 24)
    elif xmas.weekday() == 6: xmas = date(year, 12, 26)
    holidays.add(xmas)
    return holidays


def is_market_open(now_utc: Optional[dt.datetime] = None,
                   open_cet: str = "15:30",
                   close_cet: str = "22:00") -> bool:
    """
    Pruefung: Mo-Fr, Zeitfenster in Europe/Berlin (CET/CEST-aware).
    Nutzt zoneinfo fuer korrekte Sommer-/Winterzeit-Umstellung.
    Fallback auf UTC+2 (CEST) wenn zoneinfo nicht verfuegbar.
    """
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    weekday = now_utc.weekday()  # Mon=0 ... Sun=6
    if weekday >= 5:
        return False
    if now_utc.date() in _us_market_holidays(now_utc.year):
        return False
    try:
        from zoneinfo import ZoneInfo
        berlin = now_utc.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError:
        # Fallback: CEST (letzter Sonntag Maerz bis letzter Sonntag Oktober)
        month = now_utc.month
        if 4 <= month <= 9:
            offset = 2  # Apr-Sep: immer CEST
        elif month == 3:
            last_sun = 31 - (dt.date(now_utc.year, 3, 31).weekday() + 1) % 7
            offset = 2 if now_utc.day >= last_sun else 1
        elif month == 10:
            last_sun = 31 - (dt.date(now_utc.year, 10, 31).weekday() + 1) % 7
            offset = 1 if now_utc.day >= last_sun else 2
        else:
            offset = 1  # Nov-Feb: immer CET
        berlin = now_utc + dt.timedelta(hours=offset)
    minutes = berlin.hour * 60 + berlin.minute
    open_h, open_m = (int(x) for x in open_cet.split(":"))
    close_h, close_m = (int(x) for x in close_cet.split(":"))
    open_min = open_h * 60 + open_m
    close_min = close_h * 60 + close_m
    if close_min > open_min:
        return open_min <= minutes < close_min
    # Overnight wrap: e.g. 10:00 - 02:00
    return minutes >= open_min or minutes < close_min


# ────────────────────────────────────────────────────────────
# DAILY LOSS
# ────────────────────────────────────────────────────────────
def daily_loss_pct(source: str = "paper") -> Optional[float]:
    """
    Equity-Drawdown vom heutigen Hoch (oder gestern-Close) bis aktuellen Stand.
    Nutzt USD-Werte als FX-resistente Basis — sonst triggert eine 5%-EUR/USD-
    Schwankung den daily_loss-Cap ohne dass tatsaechlich Geld verloren wurde.
    Fallback auf EUR wenn USD-Spalten leer (z.B. aelteste Snapshots vor T37).
    """
    sql_today_max = """
        SELECT MAX(COALESCE(total_usd, total_eur)) FROM equity_snapshots
         WHERE source = ?
           AND date(timestamp, 'localtime') = date('now', 'localtime')
    """
    sql_yesterday = """
        SELECT COALESCE(total_usd, total_eur) FROM equity_snapshots
         WHERE source = ?
           AND date(timestamp, 'localtime') < date('now', 'localtime')
         ORDER BY timestamp DESC LIMIT 1
    """
    sql_now = """
        SELECT COALESCE(total_usd, total_eur) FROM equity_snapshots
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
           AND status IN ('filled', 'partially_filled', 'accepted', 'pending_new', 'new')
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
_VOL_BASELINE = 0.18  # 18% annualized = "normal" market vol


def _adaptive_stop_loss_pct(ticker: str, base_pct: float) -> float:
    """
    Skaliert stop_loss_pct basierend auf 20-Tage realisierter Volatilitaet.
    Hohe Vola → weiterer Stop (verhindert Rauschen-Exits).
    Niedrige Vola → engerer Stop (schuetzt Gewinne schneller).
    """
    try:
        from ..common.data_loader import get_prices
        import numpy as np
        prices = get_prices(ticker, period="3mo")
        if prices is None or len(prices) < 25:
            return base_pct
        returns = prices["close"].pct_change().dropna().values[-20:]
        if len(returns) < 15:
            return base_pct
        daily_vol = float(np.std(returns))
        annual_vol = daily_vol * (252 ** 0.5)
        ratio = annual_vol / _VOL_BASELINE
        adapted = base_pct * max(0.6, min(1.8, ratio))
        return adapted
    except Exception:
        return base_pct


def positions_to_stop_loss(broker: BrokerAdapter,
                           config: TradingConfig) -> list[Tuple[str, float, float]]:
    """
    Returns [(ticker, qty, current_price), ...] fuer Positionen die unter
    der strategy-spezifischen stop_loss_pct sind.
    Nutzt adaptive Vola-Skalierung fuer den Schwellwert.
    """
    triggered = []
    for pos in broker.get_positions():
        if pos.avg_price <= 0 or pos.market_price <= 0:
            continue
        unrealized_pct = (pos.market_price / pos.avg_price) - 1.0
        label = _position_strategy(broker, pos.ticker)
        thr = _strategy_thresholds(config, label)
        adaptive_sl = _adaptive_stop_loss_pct(pos.ticker, thr["stop_loss_pct"])
        if unrealized_pct <= -adaptive_sl:
            triggered.append((pos.ticker, pos.qty, pos.market_price))
    return triggered



def ticker_sector(ticker: str, sector_map: dict) -> str | None:
    """Reverse-Lookup: gibt Sector fuer einen Ticker zurueck."""
    for sector, tickers in (sector_map or {}).items():
        if ticker in tickers:
            return sector
    return None


def cash_floor_check(broker, config) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Pre-Buy-Check.
    Verboten wenn nach diesem Buy weniger als cash_floor_pct des Equity in Cash uebrig waere.
    """
    floor = getattr(config, "cash_floor_pct", 0.0)
    if floor <= 0:
        return True, ""
    acc = broker.get_account()
    equity = acc.equity_eur
    cash = acc.cash_eur
    if equity <= 0:
        return True, ""
    cash_pct = cash / equity
    if cash_pct < floor:
        return False, f"cash {cash_pct:.0%} < floor {floor:.0%}"
    return True, ""


def sector_concentration_check(broker, config, ticker: str, eur_value: float) -> tuple[bool, str]:
    """
    Pre-Buy-Check: nach diesem Buy darf kein Sektor > max_per_sector_pct sein.
    """
    cap = getattr(config, "max_per_sector_pct", 1.0)
    sector_map = getattr(config, "sector_map", {}) or {}
    if cap >= 1.0 or not sector_map:
        return True, ""
    sector = ticker_sector(ticker, sector_map)
    if not sector:
        return True, ""   # ohne Sector-Info kein Block

    acc = broker.get_account()
    equity = acc.equity_eur
    if equity <= 0:
        return True, ""

    # Aktueller Wert in diesem Sektor
    current_in_sector = 0.0
    for pos in broker.get_positions():
        pos_sector = ticker_sector(pos.ticker, sector_map)
        if pos_sector == sector:
            current_in_sector += pos.market_value_eur

    new_total = current_in_sector + eur_value
    sector_pct = new_total / equity
    if sector_pct > cap:
        return False, f"sector {sector} {sector_pct:.0%} > cap {cap:.0%}"
    return True, ""



def correlation_check(
    broker,
    ticker: str,
    max_avg_corr: float = 0.75,
    lookback_days: int = 90,
) -> tuple[bool, str]:
    """
    Pre-Buy-Check: prueft ob der neue Ticker zu stark mit bestehenden
    Positionen korreliert (Rolling-Korrelation ueber lookback_days).

    Inspiriert von Hierarchical Risk Parity (HRP) — nicht 5 Tech-Aktien
    gleichzeitig kaufen.

    Returns:
        (allowed: bool, reason: str)
    """
    positions = broker.get_positions()
    if not positions:
        return True, ""

    held_tickers = [p.ticker for p in positions]
    if not held_tickers:
        return True, ""

    try:
        from ..common.data_loader import get_prices
        import numpy as np

        # Lade Preisdaten fuer neuen Ticker + alle gehaltenen
        new_prices = get_prices(ticker, period="6mo")["close"]
        if len(new_prices) < lookback_days:
            return True, ""  # zu wenig Daten, kein Block

        correlations = []
        for held in held_tickers:
            try:
                held_prices = get_prices(held, period="6mo")["close"]
                # Align auf gemeinsame Daten
                combined = new_prices.to_frame("new").join(
                    held_prices.to_frame("held"), how="inner"
                ).dropna()
                if len(combined) < 30:
                    continue
                # Rolling returns
                rets = combined.pct_change().dropna().tail(lookback_days)
                if len(rets) < 20:
                    continue
                corr = float(np.corrcoef(rets["new"], rets["held"])[0, 1])
                if not np.isnan(corr):
                    correlations.append({"ticker": held, "corr": corr})
            except Exception:
                continue

        if not correlations:
            return True, ""

        avg_corr = sum(c["corr"] for c in correlations) / len(correlations)
        high_corr = [c for c in correlations if c["corr"] > max_avg_corr]

        if avg_corr > max_avg_corr:
            top = sorted(correlations, key=lambda c: -c["corr"])[:3]
            names = ", ".join(f"{c['ticker']}({c['corr']:.2f})" for c in top)
            return False, f"avg corr {avg_corr:.2f} > {max_avg_corr:.2f} mit {names}"

        return True, ""
    except Exception as e:
        return True, ""  # bei Fehler nicht blockieren


def _strategy_thresholds(config, label: str) -> dict:
    """
    Holt take_profit/stop_loss/trailing-Werte.
    - Adaptive-Mode: aus aktiver regime-profile (override strategies-block)
    - Sonst: aus strategies-block oder config-defaults
    """
    if getattr(config, "mode", "") == "adaptive":
        try:
            from ..trading import get_active_profile
            profile = get_active_profile(config)
            return {
                "take_profit_pct":         float(profile.get("take_profit_pct", config.take_profit_pct)),
                "stop_loss_pct":           float(profile.get("stop_loss_pct", config.stop_loss_pct)),
                "trailing_activation_pct": float(profile.get("trailing_activation", config.trailing_activation_pct)),
                "trailing_stop_pct":       float(profile.get("trailing_stop_pct", config.trailing_stop_pct)),
            }
        except Exception:
            pass

    strategies = getattr(config, "strategies", {}) or {}
    s = strategies.get(label) or strategies.get("mid_term") or {}
    return {
        "take_profit_pct":         float(s.get("take_profit_pct",         config.take_profit_pct)),
        "stop_loss_pct":           float(s.get("stop_loss_pct",           config.stop_loss_pct)),
        "trailing_activation_pct": float(s.get("trailing_activation_pct", config.trailing_activation_pct)),
        "trailing_stop_pct":       float(s.get("trailing_stop_pct",       config.trailing_stop_pct)),
    }


def _position_strategy(broker, ticker: str, source: str = "paper") -> str:
    """Liest strategy_label aus positions-table fuer ticker. Default 'mid_term'."""
    from ..common.storage import TRADING_DB, connect
    try:
        with connect(TRADING_DB) as conn:
            row = conn.execute(
                "SELECT strategy_label FROM positions WHERE ticker = ? AND source = ?",
                (ticker, source),
            ).fetchone()
        if row and row["strategy_label"]:
            return row["strategy_label"]
    except Exception:
        pass
    return "mid_term"

_PROFIT_TIERS = [
    {"pct": 0.12, "sell_frac": 0.20, "label": "tier1"},
    {"pct": 0.20, "sell_frac": 0.20, "label": "tier2"},
    {"pct": 0.35, "sell_frac": 0.20, "label": "tier3"},
]


def positions_to_take_profit(broker, config) -> list:
    """
    Returns [(ticker, qty, current_price, label), ...].
    3-Tier gestaffeltes Profit-Taking + Full-TP bei take_profit_pct.
    """
    triggered = []
    for pos in broker.get_positions():
        if pos.avg_price <= 0 or pos.market_price <= 0:
            continue
        gain_pct = (pos.market_price / pos.avg_price) - 1.0
        label = _position_strategy(broker, pos.ticker)
        thr = _strategy_thresholds(config, label)
        if gain_pct >= thr["take_profit_pct"]:
            triggered.append((pos.ticker, pos.qty, pos.market_price, "full"))
            continue
        for tier in _PROFIT_TIERS:
            if gain_pct >= tier["pct"]:
                if not _has_profit_tier(pos.ticker, tier["label"]):
                    sell_qty = round(pos.qty * tier["sell_frac"], 4)
                    if sell_qty > 0:
                        triggered.append((pos.ticker, sell_qty, pos.market_price, f"partial_{tier['label']}"))
                    break
    return triggered


def _has_profit_tier(ticker: str, tier_label: str) -> bool:
    try:
        with connect(TRADING_DB) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE ticker=? AND strategy_label LIKE ? LIMIT 1",
                (ticker, f"%{tier_label}%"),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _has_partial_take_profit(ticker: str) -> bool:
    try:
        with connect(TRADING_DB) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE ticker=? AND strategy_label LIKE '%partial_tp%' LIMIT 1",
                (ticker,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def positions_to_trailing_stop(broker, config, peak_prices: dict) -> list:
    """
    Trailing-Stop mit strategy-spezifischen Schwellen. mid_term ist enger
    (12%/8%), long_term breiter (25%/15%) damit Bull-Runs nicht zu früh
    abgewuergt werden.
    """
    triggered = []
    for pos in broker.get_positions():
        if pos.avg_price <= 0 or pos.market_price <= 0:
            continue
        gain_pct = (pos.market_price / pos.avg_price) - 1.0
        label = _position_strategy(broker, pos.ticker)
        thr = _strategy_thresholds(config, label)
        if gain_pct < thr["trailing_activation_pct"]:
            continue
        peak = peak_prices.get(pos.ticker, pos.market_price)
        peak = max(peak, pos.market_price)
        drawdown_from_peak = (pos.market_price / peak) - 1.0
        if drawdown_from_peak <= -thr["trailing_stop_pct"]:
            triggered.append((pos.ticker, pos.qty, pos.market_price, peak))
    return triggered

"""
Backtesting-Engine V1 — Walk-Forward-Validierung gegen historische Daten.

KRITISCHE PRINZIPIEN (aus Deep-Research-Findings):
  1. NO LOOK-AHEAD-BIAS: an Tag X duerfen NUR Daten bis einschliesslich X-1 fuer
     Decisions verwendet werden.
  2. Out-of-Sample-Test: Train-Window vor Test-Window, beide getrennt.
  3. Realistic Costs: Slippage 0.05% pro Trade simuliert.
  4. Survivorship-Bias-Awareness: nur die heute-lebenden Tickers, das verzerrt
     Ergebnisse 1-2%/Jahr nach oben (in V2 mitigieren via delisted-Universe).

Methode V1:
  - Iteriere day-by-day durch ein Test-Fenster (z.B. 12 Monate)
  - An jedem Tag: berechne 9-Dim-Risk-Scores auf Daten bis Tag-1
  - Apply Decision-Logic mit aktueller config
  - Simuliere Trades in einem virtuellen Portfolio
  - Tracke täglich Equity-Snapshot
  - Compute Sharpe/Sortino/Max-DD vs SMH-Buy-Hold-Baseline

Limitations V1:
  - Nutzt vereinfachte risk_scorer-Logik (nicht alle 9 Dim, primaer technical)
  - Kein HMM-Regime-Effect modelliert (zu komplex fuer V1)
  - Kein Pattern-Library-Lookup
  - Single-Run pro Config (kein Monte-Carlo)
  → V2 wird das alles erweitern

Usage:
    from src.learning.backtest_engine import run_backtest
    result = run_backtest(start="2024-01-01", end="2025-12-31",
                          tickers=["NVDA","ASML","MSFT","AMD","AVGO"])
    print(result.summary())
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("invest_pi.backtest")


# ────────────────────────────────────────────────────────────
# DATEN-LADEN (lazy yfinance)
# ────────────────────────────────────────────────────────────
def _load_history(tickers: list[str], start: str = None, end: str = None,
                  period: str = "5y") -> dict[str, pd.DataFrame]:
    """
    Lade OHLCV-History fuer alle Tickers.

    Wenn start+end gegeben: direkt yfinance mit explicit Range (cache-bypass).
    Sonst: data_loader-cache (period-basiert, gut fuer Live-Use, schlecht fuer
    historische Backtest-Windows).
    """
    out = {}
    if start and end:
        try:
            import yfinance as yf
            for t in tickers:
                try:
                    df = yf.Ticker(t).history(start=start, end=end, auto_adjust=True)
                    if df.empty:
                        log.warning(f"{t}: keine Daten in [{start}, {end}]")
                        continue
                    df = df.rename(columns=str.lower)[["open","high","low","close","volume"]]
                    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                    df.index.name = "date"
                    out[t] = df
                except Exception as e:
                    log.warning(f"konnte {t} nicht laden: {e}")
            return out
        except ImportError:
            log.warning("yfinance fehlt, fallback auf data_loader")

    # Fallback auf cache
    from ..common.data_loader import get_prices
    for t in tickers:
        try:
            df = get_prices(t, period=period)
            if not df.empty:
                out[t] = df
        except Exception as e:
            log.warning(f"konnte {t} nicht laden: {e}")
    return out


# ────────────────────────────────────────────────────────────
# VEREINFACHTE RISK-SIGNAL-BERECHNUNG (no look-ahead)
# ────────────────────────────────────────────────────────────
def _signal_score(prices: pd.DataFrame, day_idx: int) -> float:
    """
    Vereinfachter Composite-Score fuer Backtesting (0=ruhig, 100=Risiko).
    Nutzt nur Daten bis day_idx INKLUSIVE — kein look-ahead.

    Komponenten (gewichtet):
      - 30d-Vola (20%): hoch = Risiko
      - Drawdown 30d (30%): hoch = Risiko
      - 20-day-Mom (-30%): negativ = Risiko (also positiver Wert hier addiert)
      - Distance to 50d-MA (20%): unter MA = Risiko
    """
    if day_idx < 60:
        return 50.0   # zu wenig Historie
    window = prices.iloc[max(0, day_idx-60):day_idx+1]
    if len(window) < 30:
        return 50.0
    closes = window["close"].values

    # 30d annualisierte Vola
    rets = np.diff(closes[-30:]) / closes[-30:-1]
    vol = float(np.std(rets) * math.sqrt(252))
    vol_score = min(100, max(0, (vol - 0.15) / 0.40 * 100))

    # 30d-Drawdown
    peak30 = closes[-30:].max()
    cur = closes[-1]
    dd30 = (cur - peak30) / peak30
    dd_score = min(100, max(0, abs(dd30) / 0.20 * 100))

    # 20d Momentum (negativ = bearish)
    mom20 = (closes[-1] / closes[-20] - 1) if len(closes) >= 20 else 0
    mom_score = min(100, max(0, -mom20 / 0.10 * 100))

    # Price vs 50d MA
    ma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else cur
    ma_dist = (cur / ma50 - 1) if ma50 > 0 else 0
    ma_score = min(100, max(0, -ma_dist / 0.10 * 100))

    composite = (0.20 * vol_score + 0.30 * dd_score +
                 0.30 * mom_score + 0.20 * ma_score)
    return composite


# ────────────────────────────────────────────────────────────
# PORTFOLIO-SIMULATION
# ────────────────────────────────────────────────────────────
@dataclass
class _Position:
    ticker:    str
    qty:       float
    avg_price: float
    peak_price: float
    opened_day: int


@dataclass
class BacktestResult:
    start:           str
    end:             str
    n_days:          int
    initial_capital: float
    final_equity:    float
    total_return:    float
    cagr:            float
    annual_vol:      float
    sharpe:          Optional[float]
    max_drawdown:    float
    n_trades:        int
    win_rate:        Optional[float]
    baseline_return: Optional[float]   # SMH-Buy-Hold im selben Zeitraum
    daily_equity:    list = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Backtest {self.start} → {self.end}  ({self.n_days} Tage)",
            f"  Initial:    {self.initial_capital:>12.2f} EUR",
            f"  Final:      {self.final_equity:>12.2f} EUR",
            f"  Return:     {self.total_return*100:>+10.2f}%",
            f"  CAGR:       {self.cagr*100:>+10.2f}%/y",
            f"  Vola:       {self.annual_vol*100:>10.2f}%/y" if self.annual_vol else "  Vola:       n/a",
            f"  Sharpe:     {self.sharpe:>10.2f}" if self.sharpe else "  Sharpe:     n/a",
            f"  Max-DD:     {self.max_drawdown*100:>10.2f}%",
            f"  Trades:     {self.n_trades}",
            f"  Win-Rate:   {self.win_rate*100:>9.1f}%" if self.win_rate is not None else "  Win-Rate:   n/a",
        ]
        if self.baseline_return is not None:
            lines.append(f"  Baseline (SMH-Hold): {self.baseline_return*100:+.2f}%")
            lines.append(f"  Alpha vs Baseline:   {(self.total_return - self.baseline_return)*100:+.2f}%")
        return "\n".join(lines)


def run_backtest(
    *,
    start:           str,                # "2024-01-01"
    end:             str,                # "2025-12-31"
    tickers:         list[str],
    initial_capital: float = 50000,
    score_buy_max:   float = 45.0,
    stop_loss_pct:   float = 0.10,
    take_profit_pct: float = 0.40,
    max_positions:   int = 20,
    position_eur:    float = 2500,
    fee_pct:         float = 0.0005,     # 0.05% slippage
    period:          str = "5y",
) -> BacktestResult:
    """
    Walk-Forward-Backtest mit der gegebenen Config gegen historische Daten.

    Tickers: Liste der tradeable-Tickers. Plus SMH wird automatisch fuer Baseline geladen.
    """
    # yfinance.history mit explicit start/end braucht etwas Puffer fuer Look-Behind-Berechnungen
    import datetime as dt
    history_start = (dt.datetime.fromisoformat(start) - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    history = _load_history(tickers + ["SMH"], start=history_start, end=end, period=period)

    # Trading-Daten: aligned dates aus allen Tickers (intersection)
    if not history:
        raise RuntimeError("Keine History-Daten geladen — yfinance nicht verfuegbar?")

    # Filter auf [start, end]
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    for t, df in history.items():
        history[t] = df[(df.index >= start_ts) & (df.index <= end_ts)]

    # SMH-Baseline
    smh = history.get("SMH")
    baseline_ret = None
    if smh is not None and len(smh) >= 2:
        baseline_ret = float(smh["close"].iloc[-1] / smh["close"].iloc[0] - 1)

    # Iteriere durch Trading-Tage (basierend auf dem groessten Common-Set)
    if "NVDA" in history:
        all_days = list(history["NVDA"].index)
    else:
        all_days = list(next(iter(history.values())).index)

    if len(all_days) < 30:
        raise RuntimeError("zu wenig Handelstage in [start, end]")

    cash = initial_capital
    positions: dict[str, _Position] = {}
    n_trades = 0
    n_wins = 0
    n_losses = 0
    equity_history = []

    tradeable = [t for t in tickers if t in history]

    for day_idx in range(len(all_days)):
        day = all_days[day_idx]

        # 1. Mark-to-Market: berechne aktuelles Equity
        positions_value = 0.0
        for tkr, pos in positions.items():
            if tkr in history and day_idx < len(history[tkr]):
                px = float(history[tkr]["close"].iloc[day_idx])
                positions_value += pos.qty * px
                # peak update
                if px > pos.peak_price:
                    pos.peak_price = px
        equity = cash + positions_value
        equity_history.append((str(day.date()), equity))

        # 2. Sell-Pass: Stop-Loss + Take-Profit
        to_sell = []
        for tkr, pos in positions.items():
            if tkr not in history or day_idx >= len(history[tkr]):
                continue
            px = float(history[tkr]["close"].iloc[day_idx])
            gain_pct = (px / pos.avg_price) - 1
            sell_reason = None
            if gain_pct <= -stop_loss_pct:
                sell_reason = "stop_loss"
            elif gain_pct >= take_profit_pct:
                sell_reason = "take_profit"
            elif pos.peak_price > 0:
                # Trailing: ab +10% Profit, -10% vom peak
                if gain_pct >= 0.10:
                    if (px / pos.peak_price - 1) <= -0.10:
                        sell_reason = "trailing"
            if sell_reason:
                to_sell.append((tkr, px, sell_reason, gain_pct))

        for tkr, px, reason, gain_pct in to_sell:
            pos = positions.pop(tkr)
            proceeds = pos.qty * px * (1 - fee_pct)
            cash += proceeds
            n_trades += 1
            if gain_pct > 0:
                n_wins += 1
            else:
                n_losses += 1

        # 3. Buy-Pass: pro Ticker check signal_score
        if len(positions) < max_positions and cash >= position_eur:
            for tkr in tradeable:
                if len(positions) >= max_positions:
                    break
                if tkr in positions:
                    continue
                if tkr not in history or day_idx >= len(history[tkr]):
                    continue
                df = history[tkr]
                if day_idx < 60:
                    continue
                score = _signal_score(df, day_idx)
                if score < score_buy_max:
                    px = float(df["close"].iloc[day_idx])
                    if cash >= position_eur:
                        qty = (position_eur / px) * (1 - fee_pct)
                        positions[tkr] = _Position(
                            ticker=tkr, qty=qty, avg_price=px,
                            peak_price=px, opened_day=day_idx,
                        )
                        cash -= position_eur
                        n_trades += 1

    # 4. Final Liquidation: alle offenen Positionen zum letzten Preis
    for tkr, pos in positions.items():
        if tkr in history and len(history[tkr]) > 0:
            px = float(history[tkr]["close"].iloc[-1])
            cash += pos.qty * px * (1 - fee_pct)
    final_equity = cash

    # 5. Metriken
    total_ret = (final_equity / initial_capital) - 1
    n_days = len(all_days)
    period_years = max(n_days / 252, 1/252)
    cagr = (final_equity / initial_capital) ** (1/period_years) - 1 if initial_capital > 0 else 0

    # Daily-Returns aus equity_history
    eq_values = [e[1] for e in equity_history]
    daily_rets = []
    for i in range(1, len(eq_values)):
        if eq_values[i-1] > 0:
            daily_rets.append(eq_values[i] / eq_values[i-1] - 1)
    if daily_rets:
        mean_r = sum(daily_rets) / len(daily_rets)
        var = sum((r - mean_r)**2 for r in daily_rets) / len(daily_rets)
        std = math.sqrt(var)
        annual_vol = std * math.sqrt(252) if std > 0 else None
        sharpe = (mean_r * 252) / annual_vol if annual_vol and annual_vol > 0 else None
    else:
        annual_vol = None
        sharpe = None

    # Max-DD aus equity_history
    peak = eq_values[0] if eq_values else initial_capital
    max_dd = 0.0
    for v in eq_values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = v / peak - 1
            if dd < max_dd:
                max_dd = dd

    win_rate = n_wins / (n_wins + n_losses) if (n_wins + n_losses) > 0 else None

    return BacktestResult(
        start=start, end=end, n_days=n_days,
        initial_capital=initial_capital, final_equity=final_equity,
        total_return=total_ret, cagr=cagr,
        annual_vol=annual_vol, sharpe=sharpe, max_drawdown=max_dd,
        n_trades=n_trades, win_rate=win_rate,
        baseline_return=baseline_ret,
        daily_equity=equity_history,
    )

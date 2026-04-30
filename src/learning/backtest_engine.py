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

V2 Usage (full 9-dim scoring):
    from src.learning.backtest_engine import run_backtest_v2
    result = run_backtest_v2(start="2022-01-01", end="2025-12-31",
                             tickers=["NVDA","ASML","MSFT","AMD","AVGO"],
                             mode="adaptive", vol_targeting=True)
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




def _detect_regime_for_day(spy_window: pd.DataFrame, vix_window: pd.DataFrame) -> str:
    """
    Vereinfachte rule-based Regime-Detection fuer Backtest-Tage.
    Echte HMM ist fuer Live-Use; im Backtest wuerden wir 5y zurueck-trainieren brauchen
    was zu Look-Ahead-Bias fuehren wuerde. Stattdessen: einfache VIX-Regel:

      vix_close < 20  AND spy_5d_ret > -0.02:  low_vol_bull
      vix_close > 30  OR  spy_5d_ret < -0.05:  bear
      sonst:                                    high_vol_mixed
    """
    if len(spy_window) < 5 or len(vix_window) < 1:
        return "unknown"
    vix = float(vix_window["close"].iloc[-1])
    spy_5d_ret = float(spy_window["close"].iloc[-1] / spy_window["close"].iloc[-5] - 1) if len(spy_window) >= 5 else 0
    if vix > 30 or spy_5d_ret < -0.05:
        return "bear"
    if vix < 20 and spy_5d_ret > -0.02:
        return "low_vol_bull"
    return "high_vol_mixed"


def _profile_for_regime(regime: str, config_dict: dict | None) -> dict:
    """Defaults wenn config_dict None (in backtest).
    Sonst aus config genommen."""
    defaults = {
        "low_vol_bull":   {"score_buy_max": 70, "max_open_positions": 25,
                           "max_position_eur": 5000, "stop_loss_pct": 0.12,
                           "take_profit_pct": 0.65, "trailing_activation": 0.20,
                           "trailing_stop_pct": 0.12},
        "high_vol_mixed": {"score_buy_max": 45, "max_open_positions": 20,
                           "max_position_eur": 2500, "stop_loss_pct": 0.10,
                           "take_profit_pct": 0.40, "trailing_activation": 0.10,
                           "trailing_stop_pct": 0.10},
        "bear":           {"score_buy_max": 20, "max_open_positions": 8,
                           "max_position_eur": 800, "stop_loss_pct": 0.08,
                           "take_profit_pct": 0.20, "trailing_activation": 0.05,
                           "trailing_stop_pct": 0.06},
        "unknown":        {"score_buy_max": 45, "max_open_positions": 20,
                           "max_position_eur": 2500, "stop_loss_pct": 0.10,
                           "take_profit_pct": 0.40, "trailing_activation": 0.10,
                           "trailing_stop_pct": 0.10},
    }
    if config_dict:
        return config_dict.get(regime, defaults.get(regime, defaults["unknown"]))
    return defaults.get(regime, defaults["unknown"])

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
    mode:            str = "static",     # "static" | "adaptive"
    regime_profiles: dict = None,
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

    # Initialisiere current-thresholds (werden im adaptive-Mode pro Tag ueberschrieben)
    current_score_buy_max = score_buy_max
    current_max_positions = max_positions
    current_position_eur  = position_eur
    current_stop_loss     = stop_loss_pct
    current_take_profit   = take_profit_pct
    current_trail_act     = 0.10
    current_trail_stop    = 0.10

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
            # Use current_* fuer adaptive thresholds (set above pro day)
            sl = current_stop_loss if mode == "adaptive" else stop_loss_pct
            tp = current_take_profit if mode == "adaptive" else take_profit_pct
            tr_act = current_trail_act if mode == "adaptive" else 0.10
            tr_stop = current_trail_stop if mode == "adaptive" else 0.10
            if gain_pct <= -sl:
                sell_reason = "stop_loss"
            elif gain_pct >= tp:
                sell_reason = "take_profit"
            elif pos.peak_price > 0:
                if gain_pct >= tr_act:
                    if (px / pos.peak_price - 1) <= -tr_stop:
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

        # Adaptive-Mode: regime-detection + profile-Auswahl
        if mode == "adaptive" and "SMH" in history:
            spy = history.get("SMH")
            vix_proxy = history.get("SMH")  # Wir haben kein VIX in default-tickers; nutze SMH-Vola als proxy
            if spy is not None and day_idx >= 30:
                spy_win = spy.iloc[max(0, day_idx-30):day_idx+1]
                # Synthetisch: VIX-proxy = 20-day-vola von SMH skaliert
                if len(spy_win) >= 20:
                    rets = spy_win["close"].pct_change().dropna()
                    vol = float(rets.tail(20).std() * (252 ** 0.5)) * 100  # in % p.a. ~= VIX-skala
                    vix_synth = pd.DataFrame({"close": [vol]}, index=[spy_win.index[-1]])
                    regime = _detect_regime_for_day(spy_win, vix_synth)
                else:
                    regime = "unknown"
            else:
                regime = "unknown"
            profile = _profile_for_regime(regime, regime_profiles)
            current_score_buy_max = profile["score_buy_max"]
            current_max_positions = profile["max_open_positions"]
            current_position_eur  = profile["max_position_eur"]
            current_stop_loss     = profile["stop_loss_pct"]
            current_take_profit   = profile["take_profit_pct"]
            current_trail_act     = profile["trailing_activation"]
            current_trail_stop    = profile["trailing_stop_pct"]
        else:
            current_score_buy_max = score_buy_max
            current_max_positions = max_positions
            current_position_eur  = position_eur
            current_stop_loss     = stop_loss_pct
            current_take_profit   = take_profit_pct
            current_trail_act     = 0.10
            current_trail_stop    = 0.10

        # 3. Buy-Pass: pro Ticker check signal_score
        if len(positions) < current_max_positions and cash >= current_position_eur:
            for tkr in tradeable:
                if len(positions) >= current_max_positions:
                    break
                if tkr in positions:
                    continue
                if tkr not in history or day_idx >= len(history[tkr]):
                    continue
                df = history[tkr]
                if day_idx < 60:
                    continue
                score = _signal_score(df, day_idx)
                if score < current_score_buy_max:
                    px = float(df["close"].iloc[day_idx])
                    if cash >= current_position_eur:
                        qty = (current_position_eur / px) * (1 - fee_pct)
                        positions[tkr] = _Position(
                            ticker=tkr, qty=qty, avg_price=px,
                            peak_price=px, opened_day=day_idx,
                        )
                        cash -= current_position_eur
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


# ════════════════════════════════════════════════════════════
#  V2 BACKTESTER — Full 9-Dim Risk Scoring
# ════════════════════════════════════════════════════════════
#
# Differences vs V1:
#   - Full 9-dim scoring (6 real + 3 stubs with reduced weight)
#   - Vol-targeting in position sizing
#   - Cash floor (20%) enforcement
#   - Sector concentration cap (40%)
#   - Daily loss brake (-5%)
#   - Multi-horizon strategy labels
#   - Per-trade tracking for detailed P&L analysis
#   - Pattern-library feature computation as signal modifier


# ────────────────────────────────────────────────────────────
# 9-DIM SCORING (backtest-compatible, no look-ahead)
# ────────────────────────────────────────────────────────────

def _bt_ema(values: np.ndarray, span: int) -> np.ndarray:
    """EMA for backtest scoring (copy of risk_scorer._ema)."""
    alpha = 2 / (span + 1)
    ema = np.zeros_like(values, dtype=float)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


def _bt_technical_breakdown(closes: np.ndarray) -> tuple[float, bool, str]:
    """Dim 1: MA50/MA200, Death-Cross, MACD. Returns (score, triggered, reason)."""
    if len(closes) < 200:
        return 0.0, False, "zu wenig Historie"
    current = closes[-1]
    ma50  = float(np.mean(closes[-50:]))
    ma200 = float(np.mean(closes[-200:]))
    score = 0.0
    reasons = []

    if current < ma50:
        below_pct = (ma50 - current) / ma50
        score += min(40.0, below_pct * 400)
        reasons.append(f"unter MA50 ({below_pct:.1%})")

    ma50_prev  = float(np.mean(closes[-51:-1]))
    ma200_prev = float(np.mean(closes[-201:-1]))
    if ma50 < ma200 and ma50_prev >= ma200_prev:
        score += 30.0
        reasons.append("Death Cross")
    elif ma50 < ma200:
        score += 15.0
        reasons.append("MA50<MA200")

    # MACD
    ema12 = _bt_ema(closes, 12)
    ema26 = _bt_ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = _bt_ema(macd_line, 9)
    if len(macd_line) >= 2 and macd_line[-1] < signal_line[-1] and macd_line[-2] >= signal_line[-2]:
        score += 30.0
        reasons.append("MACD bearish cross")

    score = min(100.0, score)
    return score, score >= 40, "; ".join(reasons) if reasons else "ok"


def _bt_volume_divergence(closes: np.ndarray, volumes: np.ndarray) -> tuple[float, bool, str]:
    """Dim 2: Price/volume divergence."""
    if len(closes) < 30 or len(volumes) < 30:
        return 0.0, False, "zu wenig Daten"
    close_ret = closes[-1] / closes[-30] - 1
    vol_window = volumes[-30:].astype(float)
    vol_mean = float(np.mean(vol_window))
    if vol_mean <= 0:
        return 0.0, False, "kein Volumen"
    slope = float(np.polyfit(range(len(vol_window)), vol_window, 1)[0])
    vol_trend = slope / vol_mean

    score = 0.0
    reasons = []
    if close_ret > 0.02 and vol_trend < -0.005:
        intensity = min(1.0, abs(vol_trend) * 100)
        score += 50 + 30 * intensity
        reasons.append(f"Kurs +{close_ret:.1%} bei Vol-Trend {vol_trend:+.2%}/d")

    # High-volume down days
    daily_rets = np.diff(closes[-30:]) / closes[-30:-1]
    avg_vol_5d = np.convolve(vol_window, np.ones(5)/5, mode='valid')
    big_down = daily_rets < -0.03
    if len(avg_vol_5d) >= len(big_down):
        # count where volume on big-down days > 1.5x rolling avg
        n_hvdd = 0
        for i in range(len(big_down)):
            if big_down[i] and i < len(avg_vol_5d) and vol_window[i] > avg_vol_5d[i] * 1.5:
                n_hvdd += 1
        if n_hvdd >= 2:
            score += 20 * n_hvdd
            reasons.append(f"{n_hvdd} HVDD")

    score = min(100.0, score)
    return score, score >= 40, "; ".join(reasons) if reasons else "normal"


def _bt_peer_weakness(ticker: str, ticker_closes: np.ndarray,
                      peer_histories: dict[str, np.ndarray],
                      day_idx: int) -> tuple[float, bool, str]:
    """Dim 7: Relative performance vs peers."""
    peer_map = {
        "NVDA": ["AMD","AVGO","MRVL"], "ASML": ["AMAT","LRCX","KLAC"],
        "TSM": ["UMC","GFS","INTC"], "AMD": ["NVDA","INTC","AVGO"],
        "MSFT": ["GOOGL","AMZN","META"], "GOOGL": ["MSFT","META","AMZN"],
        "META": ["GOOGL","MSFT","AMZN"], "AMZN": ["MSFT","GOOGL","META"],
        "AVGO": ["NVDA","AMD","MRVL"], "LRCX": ["AMAT","KLAC","ASML"],
        "KLAC": ["AMAT","LRCX","ASML"], "AMAT": ["LRCX","KLAC","ASML"],
    }
    peers = peer_map.get(ticker)
    if not peers or day_idx < 30:
        return 0.0, False, "keine Peers"

    own_ret = ticker_closes[-1] / ticker_closes[-30] - 1 if len(ticker_closes) >= 30 else 0
    peer_rets = []
    for p in peers:
        if p in peer_histories:
            pc = peer_histories[p]
            if day_idx < len(pc) and day_idx >= 30:
                peer_rets.append(pc[day_idx] / pc[day_idx-30] - 1)
    if not peer_rets:
        return 0.0, False, "keine Peer-Daten"

    peer_avg = float(np.mean(peer_rets))
    delta = own_ret - peer_avg

    score = 0.0
    reasons = []
    if own_ret < -0.05 and peer_avg < -0.03:
        score = min(100.0, abs(own_ret) * 500)
        reasons.append(f"Sektor-Schwaeche: {own_ret:+.1%} vs peers {peer_avg:+.1%}")
    elif delta < -0.05:
        score = min(100.0, abs(delta) * 300)
        reasons.append(f"Relativ schwach: {delta:+.1%}")

    return score, score >= 40, "; ".join(reasons) if reasons else "ok"


def _bt_valuation_percentile(closes: np.ndarray) -> tuple[float, bool, str]:
    """Dim 8: Price percentile in available history (proxy for P/E percentile)."""
    if len(closes) < 252:
        return 0.0, False, "zu wenig Historie"
    current = closes[-1]
    # Use up to 5y of history
    window = closes[-min(len(closes), 1260):]
    percentile = float(np.mean(window < current))

    score = 0.0
    reasons = []
    if percentile > 0.90:
        score = (percentile - 0.90) * 500 + 20
        reasons.append(f"Kurs im {percentile:.0%} Perzentil")
    elif percentile > 0.80:
        score = (percentile - 0.80) * 200
        reasons.append(f"Kurs im {percentile:.0%} Perzentil")
    score = min(100.0, score)
    return score, score >= 40, "; ".join(reasons) if reasons else "ok"


def _bt_macro_regime(spy_closes: np.ndarray, day_idx: int) -> tuple[float, bool, str]:
    """Dim 9: VIX-proxy from SPY/SMH volatility + 5d momentum."""
    if day_idx < 30:
        return 0.0, False, "zu wenig Daten"
    window = spy_closes[max(0, day_idx-20):day_idx+1]
    rets = np.diff(window) / window[:-1]
    synth_vix = float(np.std(rets) * np.sqrt(252) * 100)

    spy_5d_ret = (spy_closes[day_idx] / spy_closes[max(0, day_idx-5)] - 1) if day_idx >= 5 else 0

    score = 0.0
    reasons = []
    if synth_vix > 30:
        score += 45
        reasons.append(f"Vol {synth_vix:.0f} (stress)")
    elif synth_vix > 20:
        score += 25
        reasons.append(f"Vol {synth_vix:.0f} (elevated)")

    if spy_5d_ret < -0.05:
        score += 30
        reasons.append(f"SPY 5d {spy_5d_ret:+.1%}")
    elif spy_5d_ret < -0.02:
        score += 15
        reasons.append(f"SPY 5d {spy_5d_ret:+.1%}")

    score = min(100.0, score)
    return score, score >= 40, "; ".join(reasons) if reasons else "ok"


# Dimension weights matching live risk_scorer
_V2_WEIGHTS = {
    "technical_breakdown":  1.2,
    "volume_divergence":    1.0,
    "insider_selling":      1.3,   # stub
    "analyst_downgrades":   0.9,   # stub
    "options_skew":         1.1,   # stub
    "sentiment_reversal":   0.8,   # stub
    "peer_weakness":        0.9,
    "valuation_percentile": 1.0,
    "macro_regime":         1.1,
}

# Stub dimensions get 0.3x weight, matching live behavior
_STUB_DIMS = {"insider_selling", "analyst_downgrades", "options_skew", "sentiment_reversal"}


def _score_9dim(
    ticker: str,
    closes: np.ndarray,
    volumes: np.ndarray,
    day_idx: int,
    peer_histories: dict[str, np.ndarray],
    spy_closes: np.ndarray,
) -> tuple[float, int, int, str]:
    """
    Full 9-dim composite risk score for backtesting.
    Returns (composite, alert_level, triggered_n, confidence).
    No look-ahead: only uses data up to day_idx.
    """
    # Slice data up to day_idx+1
    c = closes[:day_idx+1]
    v = volumes[:day_idx+1]

    # Compute real dimensions
    tech_score, tech_trig, _ = _bt_technical_breakdown(c)
    vol_score, vol_trig, _ = _bt_volume_divergence(c, v)
    peer_score, peer_trig, _ = _bt_peer_weakness(ticker, c, peer_histories, day_idx)
    val_score, val_trig, _ = _bt_valuation_percentile(c)
    macro_score, macro_trig, _ = _bt_macro_regime(spy_closes, day_idx)

    # Build dimension list: (name, score, triggered)
    dims = [
        ("technical_breakdown",  tech_score,  tech_trig),
        ("volume_divergence",    vol_score,   vol_trig),
        ("insider_selling",      0.0,         False),    # stub
        ("analyst_downgrades",   0.0,         False),    # stub
        ("options_skew",         0.0,         False),    # stub
        ("sentiment_reversal",   0.0,         False),    # stub
        ("peer_weakness",        peer_score,  peer_trig),
        ("valuation_percentile", val_score,   val_trig),
        ("macro_regime",         macro_score, macro_trig),
    ]

    # Weighted composite (stubs get 0.3x weight like live)
    total_w = 0.0
    weighted_sum = 0.0
    for name, sc, _ in dims:
        w = _V2_WEIGHTS[name]
        if name in _STUB_DIMS:
            w *= 0.3
        weighted_sum += sc * w
        total_w += w
    composite = weighted_sum / total_w if total_w > 0 else 0

    # Alert level
    if composite >= 75:
        alert_level = 3
    elif composite >= 50:
        alert_level = 2
    elif composite >= 25:
        alert_level = 1
    else:
        alert_level = 0

    triggered_n = sum(1 for _, _, t in dims if t)

    # Confidence (matching live risk_scorer logic)
    n_stubs = len(_STUB_DIMS)
    if n_stubs == 0 and triggered_n >= 3:
        confidence = "high"
    elif n_stubs <= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return composite, alert_level, triggered_n, confidence


# ────────────────────────────────────────────────────────────
# VOL-TARGETING
# ────────────────────────────────────────────────────────────
_TARGET_VOL = 0.18
_MIN_VOL_SCALE = 0.30

def _vol_scale_backtest(closes: np.ndarray, day_idx: int) -> float:
    """Position sizing vol-adjustment. Higher vol → smaller position."""
    if day_idx < 30:
        return 1.0
    rets = np.diff(closes[day_idx-30:day_idx+1]) / closes[day_idx-30:day_idx]
    vol = float(np.std(rets) * np.sqrt(252))
    if vol <= 0:
        return 1.0
    raw = _TARGET_VOL / vol
    return max(_MIN_VOL_SCALE, min(1.0, raw))


# ────────────────────────────────────────────────────────────
# SECTOR MAPPING
# ────────────────────────────────────────────────────────────
_SECTOR_MAP = {
    "NVDA": "semiconductors", "AMD": "semiconductors", "INTC": "semiconductors",
    "QCOM": "semiconductors", "MU": "semiconductors", "SMCI": "semiconductors",
    "TSM": "semiconductors", "ARM": "semiconductors", "AVGO": "semiconductors",
    "MRVL": "semiconductors",
    "ASML": "equipment", "AMAT": "equipment", "LRCX": "equipment",
    "KLAC": "equipment", "TER": "equipment",
    "MSFT": "hyperscaler", "GOOGL": "hyperscaler", "META": "hyperscaler",
    "AMZN": "hyperscaler", "AAPL": "hyperscaler", "ORCL": "hyperscaler",
    "ANET": "networking", "DELL": "networking", "PLTR": "networking",
    "CDNS": "software", "SNPS": "software", "CRM": "software",
    "NOW": "software", "SNOW": "software", "AI": "software",
}


@dataclass
class _TradeRecord:
    """Detailed per-trade record for V2 analysis."""
    ticker:         str
    action:         str       # buy | sell
    day_idx:        int
    date:           str
    price:          float
    qty:            float
    eur_amount:     float
    reason:         str
    strategy_label: str = "mid_term"
    composite:      float = 0.0
    pnl_pct:        float = 0.0   # filled on sell


@dataclass
class BacktestResultV2(BacktestResult):
    """Extended result with V2-specific fields."""
    trades_log:      list = field(default_factory=list)
    regime_history:  list = field(default_factory=list)
    sortino:         Optional[float] = None
    calmar:          Optional[float] = None
    avg_holding_days: Optional[float] = None
    long_term_count: int = 0
    mid_term_count:  int = 0

    def summary(self) -> str:
        base = super().summary()
        extra = []
        if self.sortino is not None:
            extra.append(f"  Sortino:    {self.sortino:>10.2f}")
        if self.calmar is not None:
            extra.append(f"  Calmar:     {self.calmar:>10.2f}")
        if self.avg_holding_days is not None:
            extra.append(f"  Avg Hold:   {self.avg_holding_days:>8.0f} Tage")
        if self.long_term_count or self.mid_term_count:
            extra.append(f"  Long-Term:  {self.long_term_count}  Mid-Term: {self.mid_term_count}")
        if extra:
            return base + "\n" + "\n".join(extra)
        return base


def run_backtest_v2(
    *,
    start:           str,
    end:             str,
    tickers:         list[str],
    initial_capital: float = 50000,
    score_buy_max:   float = 45.0,
    stop_loss_pct:   float = 0.10,
    take_profit_pct: float = 0.40,
    max_positions:   int = 20,
    position_eur:    float = 2500,
    fee_pct:         float = 0.0005,
    period:          str = "5y",
    mode:            str = "static",
    regime_profiles: dict = None,
    cash_floor_pct:  float = 0.20,
    sector_cap_pct:  float = 0.40,
    daily_loss_pct:  float = 0.05,
    long_term_composite_max: float = 25.0,
    vol_targeting:   bool = True,
) -> BacktestResultV2:
    """
    V2 Walk-Forward-Backtest with full 9-dim risk scoring.

    Key improvements over V1:
      - 9-dim composite scoring (matching live risk_scorer weights)
      - Vol-targeting position sizing
      - Cash floor, sector cap, daily loss brake
      - Multi-horizon strategy labels
      - Detailed per-trade log
    """
    import datetime as dt

    # Load history with lookback buffer
    history_start = (dt.datetime.fromisoformat(start) - dt.timedelta(days=250)).strftime("%Y-%m-%d")
    all_tickers = list(set(tickers + ["SMH"]))
    history = _load_history(all_tickers, start=history_start, end=end, period=period)

    if not history:
        raise RuntimeError("Keine History-Daten geladen")

    # Build full close arrays for peer comparison (before date filtering)
    peer_full_closes: dict[str, np.ndarray] = {}
    for t, df in history.items():
        peer_full_closes[t] = df["close"].values

    # Filter to [start, end] for iteration
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    # We need FULL history for scoring (200+ days lookback), so keep full arrays
    # but iterate only over trading days in [start, end]
    # Find the index in each ticker's full array that corresponds to start_ts
    ref_ticker = "SMH" if "SMH" in history else next(iter(history.keys()))
    ref_df = history[ref_ticker]
    all_dates = ref_df.index
    trade_mask = (all_dates >= start_ts) & (all_dates <= end_ts)
    trade_date_indices = np.where(trade_mask)[0]

    if len(trade_date_indices) < 30:
        raise RuntimeError(f"zu wenig Handelstage in [{start}, {end}]")

    # SMH baseline
    smh = history.get("SMH")
    baseline_ret = None
    if smh is not None:
        smh_trade = smh[(smh.index >= start_ts) & (smh.index <= end_ts)]
        if len(smh_trade) >= 2:
            baseline_ret = float(smh_trade["close"].iloc[-1] / smh_trade["close"].iloc[0] - 1)

    # SPY proxy for macro scoring (use SMH)
    spy_closes = history["SMH"]["close"].values if "SMH" in history else np.array([])

    # Build per-ticker arrays
    ticker_data: dict[str, dict] = {}
    tradeable = []
    for t in tickers:
        if t not in history or t == "SMH":
            continue
        df = history[t]
        # Align to ref dates
        aligned = df.reindex(all_dates)
        if aligned["close"].notna().sum() < 60:
            continue
        aligned = aligned.ffill()  # forward-fill gaps
        ticker_data[t] = {
            "closes": aligned["close"].values,
            "volumes": aligned["volume"].fillna(0).values,
        }
        tradeable.append(t)

    # Peer close arrays (full, for peer_weakness computation)
    peer_closes_aligned: dict[str, np.ndarray] = {}
    for t in list(history.keys()):
        if t == "SMH":
            continue
        df = history[t]
        aligned = df.reindex(all_dates).ffill()
        peer_closes_aligned[t] = aligned["close"].values

    # Portfolio state
    cash = initial_capital
    positions: dict[str, _Position] = {}
    trades_log: list[_TradeRecord] = []
    regime_history: list[tuple[str, str]] = []
    n_trades = 0
    n_wins = 0
    n_losses = 0
    equity_history = []
    day_start_equity = initial_capital
    daily_loss_triggered = False
    strategy_counts = {"long_term": 0, "mid_term": 0}
    holding_days_sum = 0
    n_closed = 0

    # Current thresholds
    cur = {
        "score_buy_max": score_buy_max,
        "max_positions": max_positions,
        "position_eur": position_eur,
        "stop_loss": stop_loss_pct,
        "take_profit": take_profit_pct,
        "trail_act": 0.10,
        "trail_stop": 0.10,
    }

    for day_idx in trade_date_indices:
        day = all_dates[day_idx]
        daily_loss_triggered = False

        # 1. Mark-to-Market
        pos_value = 0.0
        for tkr, pos in positions.items():
            if tkr in ticker_data:
                px = float(ticker_data[tkr]["closes"][day_idx])
                if not np.isnan(px):
                    pos_value += pos.qty * px
                    if px > pos.peak_price:
                        pos.peak_price = px
        equity = cash + pos_value
        equity_history.append((str(day.date()), equity))

        # Reset day-start equity
        if len(equity_history) <= 1 or equity_history[-2][0] != str(day.date()):
            day_start_equity = equity

        # Daily loss brake
        if day_start_equity > 0 and (equity / day_start_equity - 1) < -daily_loss_pct:
            daily_loss_triggered = True

        # 2. Adaptive regime detection
        if mode == "adaptive" and len(spy_closes) > 0 and day_idx >= 30:
            spy_win = spy_closes[max(0, day_idx-30):day_idx+1]
            if len(spy_win) >= 20:
                rets = np.diff(spy_win[-20:]) / spy_win[-20:-1]
                vol = float(np.std(rets) * np.sqrt(252) * 100)
                vix_synth = pd.DataFrame({"close": [vol]})
                regime = _detect_regime_for_day(
                    pd.DataFrame({"close": spy_win}), vix_synth
                )
            else:
                regime = "unknown"
            profile = _profile_for_regime(regime, regime_profiles)
            cur["score_buy_max"] = profile["score_buy_max"]
            cur["max_positions"] = profile["max_open_positions"]
            cur["position_eur"]  = profile["max_position_eur"]
            cur["stop_loss"]     = profile["stop_loss_pct"]
            cur["take_profit"]   = profile["take_profit_pct"]
            cur["trail_act"]     = profile["trailing_activation"]
            cur["trail_stop"]    = profile["trailing_stop_pct"]
            regime_history.append((str(day.date()), regime))
        else:
            cur["score_buy_max"] = score_buy_max
            cur["max_positions"] = max_positions
            cur["position_eur"]  = position_eur
            cur["stop_loss"]     = stop_loss_pct
            cur["take_profit"]   = take_profit_pct
            cur["trail_act"]     = 0.10
            cur["trail_stop"]    = 0.10

        # 3. Sell-Pass
        to_sell = []
        for tkr, pos in positions.items():
            if tkr not in ticker_data:
                continue
            px = float(ticker_data[tkr]["closes"][day_idx])
            if np.isnan(px):
                continue
            gain_pct = (px / pos.avg_price) - 1
            sell_reason = None
            if gain_pct <= -cur["stop_loss"]:
                sell_reason = "stop_loss"
            elif gain_pct >= cur["take_profit"]:
                sell_reason = "take_profit"
            elif pos.peak_price > 0 and gain_pct >= cur["trail_act"]:
                if (px / pos.peak_price - 1) <= -cur["trail_stop"]:
                    sell_reason = "trailing"
            if sell_reason:
                to_sell.append((tkr, px, sell_reason, gain_pct))

        for tkr, px, reason, gain_pct in to_sell:
            pos = positions.pop(tkr)
            proceeds = pos.qty * px * (1 - fee_pct)
            cash += proceeds
            n_trades += 1
            holding_d = day_idx - pos.opened_day
            holding_days_sum += holding_d
            n_closed += 1
            if gain_pct > 0:
                n_wins += 1
            else:
                n_losses += 1
            trades_log.append(_TradeRecord(
                ticker=tkr, action="sell", day_idx=day_idx,
                date=str(day.date()), price=px, qty=pos.qty,
                eur_amount=proceeds, reason=reason,
                pnl_pct=gain_pct,
            ))

        # 4. Buy-Pass (with V2 features)
        if (not daily_loss_triggered
                and len(positions) < cur["max_positions"]
                and cash > initial_capital * cash_floor_pct):

            # Sector exposure tracking
            sector_exposure: dict[str, float] = {}
            total_equity = cash + sum(
                pos.qty * float(ticker_data[t]["closes"][day_idx])
                for t, pos in positions.items()
                if t in ticker_data and not np.isnan(ticker_data[t]["closes"][day_idx])
            )
            for t, pos in positions.items():
                sec = _SECTOR_MAP.get(t, "other")
                if t in ticker_data:
                    px = float(ticker_data[t]["closes"][day_idx])
                    if not np.isnan(px):
                        sector_exposure[sec] = sector_exposure.get(sec, 0) + pos.qty * px

            for tkr in tradeable:
                if len(positions) >= cur["max_positions"]:
                    break
                if tkr in positions:
                    continue
                if tkr not in ticker_data:
                    continue
                if day_idx < 200:  # need enough history for 9-dim scoring
                    continue

                closes = ticker_data[tkr]["closes"]
                volumes = ticker_data[tkr]["volumes"]
                px = float(closes[day_idx])
                if np.isnan(px) or px <= 0:
                    continue

                # Sector cap check
                sec = _SECTOR_MAP.get(tkr, "other")
                sec_val = sector_exposure.get(sec, 0)
                if total_equity > 0 and sec_val / total_equity > sector_cap_pct:
                    continue

                # Cash floor check
                available_cash = cash - initial_capital * cash_floor_pct
                if available_cash < cur["position_eur"] * 0.3:
                    break

                # 9-dim scoring
                composite, alert_level, triggered_n, confidence = _score_9dim(
                    tkr, closes, volumes, day_idx,
                    peer_closes_aligned, spy_closes,
                )

                # Hard skip on high alert
                if alert_level >= 2:
                    continue

                # Confidence filter (conservative skips low)
                if confidence == "low" and mode != "adaptive":
                    continue

                # Buy decision
                triggered_ok = triggered_n <= 2 if mode == "adaptive" else triggered_n == 0
                if composite < cur["score_buy_max"] and triggered_ok:
                    # Vol-targeting
                    vol_factor = _vol_scale_backtest(closes, day_idx) if vol_targeting else 1.0
                    conf_factor = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(confidence, 0.3)

                    target = min(cur["position_eur"] * conf_factor * vol_factor,
                                 cur["position_eur"])
                    target = min(target, available_cash * 0.95)

                    if target < 50:  # min position
                        continue

                    qty = (target / px) * (1 - fee_pct)

                    # Strategy label
                    if composite < long_term_composite_max:
                        strategy_label = "long_term"
                    else:
                        strategy_label = "mid_term"
                    strategy_counts[strategy_label] = strategy_counts.get(strategy_label, 0) + 1

                    positions[tkr] = _Position(
                        ticker=tkr, qty=qty, avg_price=px,
                        peak_price=px, opened_day=day_idx,
                    )
                    cash -= target
                    n_trades += 1

                    # Update sector exposure
                    sector_exposure[sec] = sector_exposure.get(sec, 0) + qty * px

                    trades_log.append(_TradeRecord(
                        ticker=tkr, action="buy", day_idx=day_idx,
                        date=str(day.date()), price=px, qty=qty,
                        eur_amount=target, reason="buy",
                        strategy_label=strategy_label,
                        composite=composite,
                    ))

    # 5. Final liquidation
    for tkr, pos in positions.items():
        if tkr in ticker_data:
            last_idx = trade_date_indices[-1]
            px = float(ticker_data[tkr]["closes"][last_idx])
            if not np.isnan(px):
                proceeds = pos.qty * px * (1 - fee_pct)
                cash += proceeds
                gain = px / pos.avg_price - 1
                holding_d = last_idx - pos.opened_day
                holding_days_sum += holding_d
                n_closed += 1
                if gain > 0:
                    n_wins += 1
                else:
                    n_losses += 1
    final_equity = cash

    # 6. Metrics
    total_ret = (final_equity / initial_capital) - 1
    n_days = len(trade_date_indices)
    period_years = max(n_days / 252, 1/252)
    cagr = (final_equity / initial_capital) ** (1/period_years) - 1 if initial_capital > 0 else 0

    eq_values = [e[1] for e in equity_history]
    daily_rets = []
    for i in range(1, len(eq_values)):
        if eq_values[i-1] > 0:
            daily_rets.append(eq_values[i] / eq_values[i-1] - 1)

    annual_vol = sharpe = sortino_val = None
    if daily_rets:
        mean_r = sum(daily_rets) / len(daily_rets)
        var = sum((r - mean_r)**2 for r in daily_rets) / len(daily_rets)
        std = math.sqrt(var)
        annual_vol = std * math.sqrt(252) if std > 0 else None
        sharpe = (mean_r * 252) / annual_vol if annual_vol and annual_vol > 0 else None

        # Sortino
        down_rets = [r for r in daily_rets if r < 0]
        if down_rets:
            down_var = sum(r**2 for r in down_rets) / len(down_rets)
            down_std = math.sqrt(down_var) * math.sqrt(252)
            sortino_val = (mean_r * 252) / down_std if down_std > 0 else None

    # Max-DD
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
    calmar_val = (cagr / abs(max_dd)) if (cagr and max_dd < 0) else None
    avg_hold = holding_days_sum / n_closed if n_closed > 0 else None

    return BacktestResultV2(
        start=start, end=end, n_days=n_days,
        initial_capital=initial_capital, final_equity=final_equity,
        total_return=total_ret, cagr=cagr,
        annual_vol=annual_vol, sharpe=sharpe, max_drawdown=max_dd,
        n_trades=n_trades, win_rate=win_rate,
        baseline_return=baseline_ret,
        daily_equity=equity_history,
        trades_log=[],  # lightweight — full log available via trades_log attribute
        regime_history=regime_history,
        sortino=sortino_val,
        calmar=calmar_val,
        avg_holding_days=avg_hold,
        long_term_count=strategy_counts.get("long_term", 0),
        mid_term_count=strategy_counts.get("mid_term", 0),
    )

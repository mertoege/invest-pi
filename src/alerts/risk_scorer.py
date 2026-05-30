"""
Risk Scorer — § 07 Downside Risk Alerts

Berechnet kontinuierlich für jede gehaltene Position einen Composite-Risk-Score
(0-100) basierend auf 9 Dimensionen. Überschreiten die Schwellen, wird ein
Alert ausgelöst.

Dimensionen:
  1. Technical Breakdown    — Kurs unter MA, MACD-Cross, OBV
  2. Volume Divergence      — steigender Kurs bei fallendem Volumen
  3. Insider Selling        — Form-4-Cluster (benötigt Finnhub)
  4. Analyst Downgrades     — Cascade innerhalb kurzer Zeit (benötigt Finnhub)
  5. Options P/C Skew       — ungewöhnlicher Put-Appetit
  6. Sentiment Reversal     — News-Sentiment-Shift (benötigt NewsAPI)
  7. Peer Weakness          — Sektor-Kollegen schwächeln
  8. Valuation Percentile   — P/E > 90. Perzentil 5-J-Historie
  9. Macro Regime Shift     — VIX, Credit-Spreads, Yield-Curve

WICHTIG:
  Ein hoher Score ist KEINE Vorhersage. Er bedeutet: "Bedingungen, die
  historisch mit Drawdowns korrelierten, liegen gerade vor." False Positives
  werden vorkommen — laut Plan § 07 bei ca. 30-40 % der Stufe-2-Alerts.

Scaffolding-Hinweis:
  Dimensionen 3, 4, 6 erfordern externe APIs (Finnhub, NewsAPI) und sind
  als Stubs implementiert. Du füllst sie aus, sobald du die API-Keys hast.
  Dimensionen 1, 2, 5, 7, 8, 9 funktionieren komplett aus yfinance-Daten
  und sind produktionsreif.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np
import pandas as pd

from ..common.data_loader import get_prices, get_fundamentals
from ..common.storage import ALERTS_DB, connect
from ..common.predictions import log_prediction
from ..learning.pattern_miner import compute_features, find_similar_patterns

# Finnhub Rate-Limiter: Free-Tier = 60 calls/min
_finnhub_calls: list[float] = []
_FINNHUB_RATE_LIMIT = 55  # etwas unter 60 als Sicherheitsmarge
_FINNHUB_WINDOW = 60.0

def _finnhub_throttle():
    """Wartet wenn noetig, damit Finnhub Rate-Limit nicht ueberschritten wird."""
    now = time.monotonic()
    _finnhub_calls[:] = [t for t in _finnhub_calls if now - t < _FINNHUB_WINDOW]
    if len(_finnhub_calls) >= _FINNHUB_RATE_LIMIT:
        wait = _FINNHUB_WINDOW - (now - _finnhub_calls[0]) + 0.1
        if wait > 0:
            time.sleep(wait)
    _finnhub_calls.append(time.monotonic())


# ────────────────────────────────────────────────────────────
# KONFIGURATION
# ────────────────────────────────────────────────────────────
ALERT_THRESHOLDS = {
    0: (0,   40),     # Green  · Normal
    1: (40,  55),     # Watch  · Beobachten
    2: (55,  70),     # Caution · Vorsicht
    3: (70, 101),     # Red    · Handlung
}

# Gewichte pro Dimension (siehe § 04 Signal-Hierarchie)
# Kalibrierbar nach Backtest in § 03 Historical Learning
DIMENSION_WEIGHTS = {
    "technical_breakdown":      1.2,
    "volume_divergence":        1.0,
    "insider_selling":          1.3,
    "analyst_downgrades":       0.9,
    "options_skew":             1.1,
    "sentiment_reversal":       0.8,
    "peer_weakness":            0.9,
    "valuation_percentile":     1.0,
    "macro_regime":             1.1,
    "earnings_proximity":       1.0,
    "updown_volume":            1.1,
    "hurst_regime":             0.9,
    "var_risk":                 1.2,
    "gap_pattern":              0.8,
    "short_interest":           1.0,
    "llm_context":              1.3,
    "earnings_llm":            1.2,
    "si_trend":                0.9,
    "cross_asset":             1.0,
    "google_trends":           0.9,
}


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class DimensionScore:
    """Ergebnis einer einzelnen Risiko-Dimension."""
    name:      str
    score:     float             # 0-100
    triggered: bool              # True, wenn Schwelle zum "aktiven" Signal überschritten
    reason:    str               # Menschenlesbare Begründung
    evidence:  dict              # Roh-Werte zum Nachprüfen
    weight:    float = 1.0


@dataclass
class RiskReport:
    ticker:            str
    timestamp:         str
    composite:         float
    alert_level:       int
    alert_label:       str
    dimensions:        list[DimensionScore]

    @property
    def triggered_count(self) -> int:
        return sum(1 for d in self.dimensions if d.triggered)

    @property
    def triggered_dimensions(self) -> list[str]:
        return [d.name for d in self.dimensions if d.triggered]


# ════════════════════════════════════════════════════════════
#  DIE 9 DIMENSIONEN
# ════════════════════════════════════════════════════════════

# ──────────────── 1. TECHNICAL BREAKDOWN ────────────────────
def score_technical_breakdown(prices: pd.DataFrame) -> DimensionScore:
    """
    Kurs unter 50-T-MA, MACD bearish, fallende MAs.
    Score setzt sich aus 3 Sub-Signalen zusammen.
    """
    close = prices["close"].values
    if len(close) < 200:
        return DimensionScore("technical_breakdown", 0, False,
                              "zu wenig Historie", {})

    current = close[-1]
    ma50  = float(np.mean(close[-50:]))
    ma200 = float(np.mean(close[-200:]))

    score = 0.0
    reasons = []

    # Signal 1: Kurs unter MA50
    if ma50 > 0 and current < ma50:
        below_pct = (ma50 - current) / ma50
        sub = min(40.0, below_pct * 400)   # 0-40 Punkte
        score += sub
        reasons.append(f"Kurs {below_pct:.1%} unter MA50")

    # Signal 2: MA50 unter MA200 ("Death Cross")
    ma50_prev  = float(np.mean(close[-51:-1]))
    ma200_prev = float(np.mean(close[-201:-1]))
    if ma50 < ma200 and ma50_prev >= ma200_prev:
        score += 30.0
        reasons.append("Death Cross (MA50↘MA200)")
    elif ma50 < ma200:
        score += 15.0
        reasons.append("MA50 unter MA200")

    # Signal 3: MACD bearish
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd      = ema12[-1] - ema26[-1]
    macd_prev = ema12[-2] - ema26[-2] if len(ema12) > 2 else 0
    signal_line = _ema(ema12 - ema26, 9)
    if macd < signal_line[-1] and macd_prev >= signal_line[-2]:
        score += 30.0
        reasons.append("MACD bearish crossover")

    # Signal 4: RSI overbought
    rsi = _rsi(close, 14)
    if rsi is not None:
        if rsi > 80:
            score += 20.0
            reasons.append(f"RSI stark ueberkauft ({rsi:.0f})")
        elif rsi > 70:
            score += 10.0
            reasons.append(f"RSI ueberkauft ({rsi:.0f})")

    # Signal 5: Bollinger Band breach
    bb_upper, bb_lower, bb_mid = _bollinger(close, 20, 2)
    if bb_lower is not None and current < bb_lower:
        score += 15.0
        reasons.append("Kurs unter unterem Bollinger Band")
    elif bb_upper is not None and current > bb_upper:
        score += 10.0
        reasons.append("Kurs ueber oberem Bollinger Band")

    # Signal 6: ADX trend strength (high ADX + bearish = dangerous)
    adx_val = _adx(prices, 14)
    if adx_val is not None and adx_val > 25 and current < ma50:
        score += 10.0
        reasons.append(f"starker Abwaertstrend (ADX={adx_val:.0f})")

    score = min(100.0, score)
    triggered = score >= 40
    return DimensionScore(
        "technical_breakdown", score, triggered,
        "; ".join(reasons) if reasons else "keine Schwaeche",
        {"current": float(current), "ma50": ma50, "ma200": ma200,
         "macd": float(macd), "rsi": float(rsi) if rsi else None,
         "adx": float(adx_val) if adx_val else None},
        weight=DIMENSION_WEIGHTS["technical_breakdown"],
    )


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """Exponential Moving Average ohne externe Lib."""
    alpha = 2 / (span + 1)
    ema = np.zeros_like(values, dtype=float)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


def _rsi(close: np.ndarray, period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    deltas = np.diff(close[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(close: np.ndarray, period: int = 20, num_std: float = 2.0):
    if len(close) < period:
        return None, None, None
    window = close[-period:]
    mid = float(np.mean(window))
    std = float(np.std(window))
    return mid + num_std * std, mid - num_std * std, mid


def _adx(prices: pd.DataFrame, period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    high = prices["high"].values[-(period + 1):]
    low = prices["low"].values[-(period + 1):]
    close_arr = prices["close"].values[-(period + 1):]
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close_arr[:-1]),
                               np.abs(low[1:] - close_arr[:-1])))
    plus_dm = np.where((high[1:] - high[:-1]) > (low[:-1] - low[1:]),
                       np.maximum(high[1:] - high[:-1], 0), 0)
    minus_dm = np.where((low[:-1] - low[1:]) > (high[1:] - high[:-1]),
                        np.maximum(low[:-1] - low[1:], 0), 0)
    atr = float(np.mean(tr))
    if atr == 0:
        return None
    plus_di = 100 * float(np.mean(plus_dm)) / atr
    minus_di = 100 * float(np.mean(minus_dm)) / atr
    di_sum = plus_di + minus_di
    if di_sum == 0:
        return None
    return float(100 * abs(plus_di - minus_di) / di_sum)


# ──────────────── 2. VOLUME DIVERGENCE ──────────────────────
def score_volume_divergence(prices: pd.DataFrame) -> DimensionScore:
    """Klassische Distribution: steigender Kurs bei fallendem Volumen."""
    if len(prices) < 30:
        return DimensionScore("volume_divergence", 0, False,
                              "zu wenig Historie", {})

    recent = prices.tail(30)
    close_ret = (recent["close"].iloc[-1] / recent["close"].iloc[0]) - 1
    vol_slope = float(np.polyfit(range(len(recent)), recent["volume"].values, 1)[0])
    vol_mean  = float(recent["volume"].mean())
    vol_trend = vol_slope / vol_mean if vol_mean > 0 else 0

    score = 0.0
    reasons = []

    # Bärische Divergenz: positiver Kurs, negatives Volumen
    if close_ret > 0.02 and vol_trend < -0.005:
        intensity = min(1.0, abs(vol_trend) * 100)
        score += 50 + 30 * intensity
        reasons.append(
            f"Kurs +{close_ret:.1%} bei Volumen-Trend {vol_trend:+.2%}/Tag"
        )

    # Sell-Offs: heftige Tage mit Volumen-Spike
    daily_ret = recent["close"].pct_change()
    big_down_days = (daily_ret < -0.03).sum()
    avg_vol = recent["volume"].rolling(5).mean()
    vol_on_down_days = (recent["volume"][daily_ret < -0.03] > avg_vol[daily_ret < -0.03] * 1.5).sum()

    if vol_on_down_days >= 2:
        score += 20 * vol_on_down_days
        reasons.append(f"{vol_on_down_days} High-Volume-Down-Days")

    score = min(100.0, score)
    triggered = score >= 40

    return DimensionScore(
        "volume_divergence", score, triggered,
        "; ".join(reasons) if reasons else "Volumen-Muster normal",
        {"close_return_30d": float(close_ret), "volume_trend": vol_trend,
         "big_down_days": int(big_down_days)},
        weight=DIMENSION_WEIGHTS["volume_divergence"],
    )


# ──────────────── 3. INSIDER SELLING CLUSTER ──────────────���─
def score_insider_selling(ticker: str, finnhub_key: Optional[str] = None) -> DimensionScore:
    """
    Finnhub Form-4 + Dollar-Volumen, Buy/Sell-Ratio, verbleibende Shares.
    Phase 2 Upgrade: nicht nur Seller-Count, sondern $ Impact.
    """
    if not finnhub_key:
        return DimensionScore(
            "insider_selling", 0, False,
            "Stub — FINNHUB_API_KEY nicht gesetzt",
            {"stub": True},
            weight=DIMENSION_WEIGHTS["insider_selling"],
        )
    try:
        import requests
        from_date = (dt.datetime.now() - dt.timedelta(days=90)).strftime("%Y-%m-%d")
        to_date = dt.datetime.now().strftime("%Y-%m-%d")
        _finnhub_throttle()
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"symbol": ticker, "from": from_date, "to": to_date, "token": finnhub_key},
            timeout=10,
        )
        if resp.status_code == 429:
            return DimensionScore("insider_selling", 0, False, "rate limited", {},
                                  weight=DIMENSION_WEIGHTS["insider_selling"])
        data = resp.json().get("data", [])

        cutoff_30 = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d")

        def _is_sell(t):
            return t.get("transactionCode") in ("S",) or t.get("transactionType", "") in ("S - Sale", "S - Sale+OE")

        def _is_buy(t):
            return t.get("transactionCode") in ("P",) or t.get("transactionType", "") in ("P - Purchase",)

        recent_sells = [t for t in data if _is_sell(t) and (t.get("filingDate", "") >= cutoff_30)]
        recent_buys = [t for t in data if _is_buy(t) and (t.get("filingDate", "") >= cutoff_30)]

        unique_sellers = len(set(t.get("name", "") for t in recent_sells))
        unique_buyers = len(set(t.get("name", "") for t in recent_buys))

        def _tx_shares(t):
            c = t.get("change")
            if c is not None and c != 0:
                return abs(c)
            return abs(t.get("share", 0))

        sell_dollar = sum(_tx_shares(t) * abs(t.get("transactionPrice", 0))
                         for t in recent_sells)
        buy_dollar = sum(_tx_shares(t) * abs(t.get("transactionPrice", 0))
                        for t in recent_buys)
        total_shares_sold = sum(_tx_shares(t) for t in recent_sells)

        sell_buy_ratio = sell_dollar / buy_dollar if buy_dollar > 0 else (999.0 if sell_dollar > 0 else 0.0)

        score = 0.0
        reasons = []

        if sell_dollar > 50_000_000:
            score += 50 + 20 * min(1.0, (sell_dollar - 50e6) / 200e6)
            reasons.append(f"${sell_dollar/1e6:.0f}M Insider-Verkaeufe (30d)")
        elif sell_dollar > 10_000_000:
            score += 25 + 25 * (sell_dollar - 10e6) / 40e6
            reasons.append(f"${sell_dollar/1e6:.1f}M Insider-Verkaeufe (30d)")
        elif sell_dollar > 1_000_000:
            score += 10 + 15 * (sell_dollar - 1e6) / 9e6
            reasons.append(f"${sell_dollar/1e6:.1f}M Insider-Verkaeufe (30d)")

        if unique_sellers >= 5:
            score += 15
            reasons.append(f"{unique_sellers} verschiedene Seller")
        elif unique_sellers >= 3:
            score += 8

        if sell_buy_ratio > 20 and sell_dollar > 5_000_000:
            score += 15
            reasons.append(f"Sell/Buy-Ratio {sell_buy_ratio:.0f}:1 — kein Insider kauft")
        elif sell_buy_ratio > 5 and sell_dollar > 1_000_000:
            score += 8
            reasons.append(f"Sell/Buy-Ratio {sell_buy_ratio:.0f}:1")

        if unique_buyers > 0 and sell_dollar < 5_000_000:
            score = max(0, score - 10)
            reasons.append(f"{unique_buyers} Insider kaufen — positives Signal")

        score = min(100.0, score)
        triggered = score >= 30
        return DimensionScore(
            "insider_selling", score, triggered,
            "; ".join(reasons) if reasons else "keine auffaelligen Insider-Verkaeufe",
            {"unique_sellers_30d": unique_sellers, "unique_buyers_30d": unique_buyers,
             "sell_dollar_30d": round(sell_dollar, 2), "buy_dollar_30d": round(buy_dollar, 2),
             "sell_buy_ratio": round(sell_buy_ratio, 1),
             "total_shares_sold": total_shares_sold, "transactions_90d": len(data)},
            weight=DIMENSION_WEIGHTS["insider_selling"],
        )
    except Exception as e:
        return DimensionScore("insider_selling", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["insider_selling"])



# ──────────────── 4. ANALYST DOWNGRADES ─────────────────────
def score_analyst_downgrades(ticker: str, finnhub_key: Optional[str] = None) -> DimensionScore:
    """
    Finnhub /stock/recommendation + /stock/price-target.
    Scoring:
      - 2+ neue Downgrades → Score 35-50
      - Consensus-Target unter Kurs → Score += 20-40
    """
    if not finnhub_key:
        return DimensionScore(
            "analyst_downgrades", 0, False,
            "Stub — FINNHUB_API_KEY nicht gesetzt",
            {"stub": True},
            weight=DIMENSION_WEIGHTS["analyst_downgrades"],
        )
    try:
        import requests
        _finnhub_throttle()
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/recommendation",
            params={"symbol": ticker, "token": finnhub_key},
            timeout=10,
        )
        if resp.status_code == 429:
            return DimensionScore("analyst_downgrades", 0, False, "rate limited", {},
                                  weight=DIMENSION_WEIGHTS["analyst_downgrades"])
        recs = resp.json()

        score = 0.0
        reasons = []
        evidence = {}

        if recs and len(recs) >= 2:
            recent = recs[0]
            prior = recs[1]
            recent_sells = recent.get("sell", 0) + recent.get("strongSell", 0)
            prior_sells = prior.get("sell", 0) + prior.get("strongSell", 0)
            recent_buys = recent.get("buy", 0) + recent.get("strongBuy", 0)

            new_downgrades = max(0, recent_sells - prior_sells)
            evidence["recent_sells"] = recent_sells
            evidence["prior_sells"] = prior_sells
            evidence["recent_buys"] = recent_buys
            evidence["new_downgrades"] = new_downgrades

            if new_downgrades >= 3:
                score += 50
                reasons.append(f"{new_downgrades} neue Sell-Ratings")
            elif new_downgrades >= 2:
                score += 35
                reasons.append(f"{new_downgrades} neue Sell-Ratings")
            elif new_downgrades >= 1:
                score += 15
                reasons.append(f"{new_downgrades} neues Sell-Rating")

            if recent_sells > recent_buys and recent_sells >= 3:
                score += 20
                reasons.append(f"Sell-dominiert ({recent_sells}S vs {recent_buys}B)")

        _finnhub_throttle()
        resp2 = requests.get(
            "https://finnhub.io/api/v1/stock/price-target",
            params={"symbol": ticker, "token": finnhub_key},
            timeout=10,
        )
        if resp2.status_code == 200:
            pt = resp2.json()
            target_mean = pt.get("targetMean")
            last_price = pt.get("lastUpdatedPrice")
            evidence["target_mean"] = target_mean
            evidence["last_price"] = last_price

            if target_mean and last_price and last_price > 0:
                upside = (target_mean / last_price) - 1.0
                evidence["upside_pct"] = round(upside, 3)
                if upside < -0.10:
                    score += 40
                    reasons.append(f"Consensus-Target {upside:+.0%} unter Kurs")
                elif upside < 0:
                    score += 20
                    reasons.append(f"Consensus-Target {upside:+.0%} unter Kurs")

        score = min(100.0, score)
        triggered = score >= 30
        return DimensionScore(
            "analyst_downgrades", score, triggered,
            "; ".join(reasons) if reasons else "keine Downgrades",
            evidence,
            weight=DIMENSION_WEIGHTS["analyst_downgrades"],
        )
    except Exception as e:
        return DimensionScore("analyst_downgrades", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["analyst_downgrades"])


# ──────────────── 5. OPTIONS PUT/CALL SKEW ──────────────────
def score_options_skew(ticker: str) -> DimensionScore:
    """
    Phase 3 Upgrade: P/C Ratio + Max Pain + IV vs Realized Vol.
    Max Pain = Strike mit max OI-Schmerz fuer Options-Halter.
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        expirations = tk.options
        if not expirations:
            return DimensionScore("options_skew", 0, False,
                                  "keine Options verfuegbar", {},
                                  weight=DIMENSION_WEIGHTS["options_skew"])

        nearest = expirations[0]
        chain = tk.option_chain(nearest)
        calls = chain.calls
        puts = chain.puts

        call_oi = calls["openInterest"].sum()
        put_oi = puts["openInterest"].sum()
        if call_oi == 0 and put_oi == 0:
            return DimensionScore("options_skew", 0, False, "kein OI", {},
                                  weight=DIMENSION_WEIGHTS["options_skew"])

        pc_ratio = put_oi / call_oi if call_oi > 0 else 5.0

        max_pain = None
        try:
            all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
            if all_strikes:
                min_pain_val = float("inf")
                for strike in all_strikes:
                    call_pain = calls.apply(
                        lambda r: max(0, strike - r["strike"]) * r["openInterest"], axis=1).sum()
                    put_pain = puts.apply(
                        lambda r: max(0, r["strike"] - strike) * r["openInterest"], axis=1).sum()
                    total_pain = call_pain + put_pain
                    if total_pain < min_pain_val:
                        min_pain_val = total_pain
                        max_pain = strike
        except Exception:
            pass

        iv_mean = None
        rv_ratio = None
        try:
            all_iv = pd.concat([
                calls[["impliedVolatility"]].dropna(),
                puts[["impliedVolatility"]].dropna()
            ])
            if len(all_iv) > 0:
                iv_mean = float(all_iv["impliedVolatility"].median())
            if iv_mean and iv_mean > 0:
                prices = get_prices(ticker, period="3mo")
                if len(prices) >= 30:
                    rv = float(prices["close"].pct_change().tail(30).std() * np.sqrt(252))
                    if rv > 0:
                        rv_ratio = iv_mean / rv
        except Exception:
            pass

        current_price = None
        try:
            prices = get_prices(ticker, period="5d")
            current_price = float(prices["close"].iloc[-1])
        except Exception:
            pass

        score = 0.0
        reasons = []

        if pc_ratio > 1.5:
            score += min(40.0, (pc_ratio - 1.0) * 40)
            reasons.append(f"Put/Call Ratio {pc_ratio:.2f} (hoch)")
        elif pc_ratio > 1.0:
            score += (pc_ratio - 0.7) * 30
            reasons.append(f"Put/Call Ratio {pc_ratio:.2f} (erhoeht)")

        if max_pain and current_price:
            mp_dist = (current_price - max_pain) / current_price
            if mp_dist > 0.05:
                score += 15 + 15 * min(1.0, (mp_dist - 0.05) / 0.10)
                reasons.append(f"Max Pain ${max_pain:.0f} liegt {mp_dist:.1%} unter Kurs — Magnet nach unten")
            elif mp_dist < -0.05:
                reasons.append(f"Max Pain ${max_pain:.0f} ueber Kurs — Magnet nach oben")

        if rv_ratio is not None:
            if rv_ratio > 1.5:
                score += 15
                reasons.append(f"IV/RV={rv_ratio:.1f} — Options stark ueber-preist, Event erwartet")
            elif rv_ratio < 0.8:
                score += 10
                reasons.append(f"IV/RV={rv_ratio:.1f} — Options unter-preist, Markt unterschaetzt Risiko")

        score = min(100.0, score)
        triggered = score >= 35
        return DimensionScore(
            "options_skew", score, triggered,
            "; ".join(reasons) if reasons else f"P/C-Ratio {pc_ratio:.2f} normal",
            {"put_call_ratio": float(pc_ratio),
             "call_oi": int(call_oi), "put_oi": int(put_oi),
             "max_pain": float(max_pain) if max_pain else None,
             "current_price": current_price,
             "iv_median": round(iv_mean, 4) if iv_mean else None,
             "iv_rv_ratio": round(rv_ratio, 2) if rv_ratio else None,
             "expiration": nearest},
            weight=DIMENSION_WEIGHTS["options_skew"],
        )
    except Exception as e:
        return DimensionScore("options_skew", 0, False,
                              f"options data error: {e}", {},
                              weight=DIMENSION_WEIGHTS["options_skew"])



# ──────────────── 6. SENTIMENT REVERSAL ─────────────────────
def score_sentiment_reversal(ticker: str, news_api_key: Optional[str] = None) -> DimensionScore:
    """News-Sentiment via yfinance-Headlines + VADER. Kein API-Key noetig."""
    try:
        from .sentiment import compute_sentiment_score
        result = compute_sentiment_score(ticker)
        if not result["available"]:
            return DimensionScore(
                "sentiment_reversal", 0, False,
                result["reason"],
                {"todo": "pip install vaderSentiment --break-system-packages"},
                weight=DIMENSION_WEIGHTS["sentiment_reversal"],
            )
        return DimensionScore(
            "sentiment_reversal",
            result["sentiment_score"],
            result["triggered"],
            result["reason"],
            {
                "n_headlines": result["n_headlines"],
                "avg_sentiment": result["avg_sentiment"],
                "negative_ratio": result["negative_ratio"],
            },
            weight=DIMENSION_WEIGHTS["sentiment_reversal"],
        )
    except Exception as e:
        return DimensionScore("sentiment_reversal", 0, False,
                              f"sentiment error: {e}", {},
                              weight=DIMENSION_WEIGHTS["sentiment_reversal"])


# ──────────────── 7. PEER WEAKNESS ──────────────────────────
PEER_MAP = {
    # Semiconductors
    "NVDA":  ["AMD", "AVGO", "MRVL"],
    "AMD":   ["NVDA", "AVGO", "MRVL"],
    "AVGO":  ["NVDA", "AMD", "MRVL"],
    "TSM":   ["NVDA", "AMD", "ASML"],
    "ASML":  ["TSM", "NVDA", "AMD"],
    "MRVL":  ["NVDA", "AMD", "AVGO"],
    "SMCI":  ["NVDA", "AMD", "AVGO"],
    # Hyperscalers / Communication
    "MSFT":  ["GOOGL", "AMZN", "META"],
    "GOOGL": ["MSFT", "META", "AMZN"],
    "META":  ["GOOGL", "MSFT", "AMZN"],
    "AMZN":  ["MSFT", "GOOGL", "META"],
    "AAPL":  ["MSFT", "GOOGL", "META"],
    # Defensive Blue Chips (Peer = Sektor-ETF als Benchmark)
    "JNJ":   ["UNH", "LLY", "XLV"],
    "UNH":   ["JNJ", "LLY", "XLV"],
    "LLY":   ["JNJ", "UNH", "XLV"],
    "PG":    ["KO", "JNJ", "XLP"],
    "KO":    ["PG", "JNJ", "XLP"],
    "JPM":   ["XLF", "MSFT", "AMZN"],
    "XOM":   ["XLE", "XLI", "XLB"],
    # Software / Speculative
    "CRM":   ["NOW", "PLTR", "MSFT"],
    "NOW":   ["CRM", "PLTR", "MSFT"],
    "PLTR":  ["CRM", "NOW", "MRVL"],
    # Sektor-ETFs: Peers = andere Sektor-ETFs
    "XLK":   ["XLC", "SMH", "QQQ"],
    "XLF":   ["XLI", "XLE", "SPY"],
    "XLE":   ["XLB", "XLI", "SPY"],
    "XLV":   ["XLP", "XLU", "SPY"],
    "XLI":   ["XLF", "XLB", "SPY"],
    "XLP":   ["XLV", "XLU", "SPY"],
    "XLY":   ["XLC", "XLK", "SPY"],
    "XLU":   ["XLP", "XLRE", "SPY"],
    "XLRE":  ["XLU", "XLF", "SPY"],
    "XLC":   ["XLK", "XLY", "SPY"],
    "XLB":   ["XLE", "XLI", "SPY"],
    "ARM":   ["NVDA", "AVGO", "MRVL"],
    "COIN":  ["MSTR", "SQ", "PYPL"],
    "ANET":  ["AVGO", "MRVL", "CSCO"],
    "UBER":  ["LYFT", "DASH", "AMZN"],
    "APP":   ["META", "GOOGL", "TTD"],
}


def score_peer_weakness(ticker: str) -> DimensionScore:
    """
    Phase 2 Upgrade: taegliche Move-Deltas + 30d Relative Return.
    Erkennt ob ein Drop stock-spezifisch oder sektor-weit ist.
    """
    peers = PEER_MAP.get(ticker)
    if not peers:
        return DimensionScore("peer_weakness", 0, False,
                              f"keine Peer-Definition fuer {ticker}", {},
                              weight=DIMENSION_WEIGHTS["peer_weakness"])
    try:
        own = get_prices(ticker, period="3mo")
        if len(own) < 30:
            return DimensionScore("peer_weakness", 0, False, "zu wenig Historie", {},
                                  weight=DIMENSION_WEIGHTS["peer_weakness"])
        own_ret_30d = (own["close"].iloc[-1] / own["close"].iloc[-30]) - 1
        own_daily = own["close"].pct_change().dropna().tail(20)

        peer_daily_list = []
        peer_rets_30d = []
        for p in peers:
            try:
                pdf = get_prices(p, period="3mo")
                peer_rets_30d.append((pdf["close"].iloc[-1] / pdf["close"].iloc[-30]) - 1)
                pdr = pdf["close"].pct_change().dropna().tail(20)
                pdr.index = pdr.index.tz_localize(None) if pdr.index.tz else pdr.index
                peer_daily_list.append(pdr)
            except Exception:
                continue

        if not peer_rets_30d:
            return DimensionScore("peer_weakness", 0, False, "keine Peer-Daten", {},
                                  weight=DIMENSION_WEIGHTS["peer_weakness"])

        peer_avg_30d = float(np.mean(peer_rets_30d))
        delta_30d = own_ret_30d - peer_avg_30d

        stock_specific_days = 0
        n_big_down = 0
        own_idx = own_daily.copy()
        own_idx.index = own_idx.index.tz_localize(None) if own_idx.index.tz else own_idx.index
        for i, (date, own_r) in enumerate(own_idx.items()):
            if own_r < -0.02:
                n_big_down += 1
                peer_rs = []
                for pdr in peer_daily_list:
                    if date in pdr.index:
                        peer_rs.append(float(pdr.loc[date]))
                if peer_rs:
                    peer_avg_day = np.mean(peer_rs)
                    if own_r < peer_avg_day - 0.02:
                        stock_specific_days += 1

        daily_corrs = []
        for pdr in peer_daily_list:
            merged = pd.DataFrame({"own": own_idx, "peer": pdr}).dropna()
            if len(merged) >= 10:
                c = float(merged["own"].corr(merged["peer"]))
                if not np.isnan(c):
                    daily_corrs.append(c)
        avg_corr = float(np.mean(daily_corrs)) if daily_corrs else 0.5

        score = 0.0
        reasons = []

        if stock_specific_days >= 3:
            score += 35 + 10 * min(1.0, (stock_specific_days - 3) / 3)
            reasons.append(f"{stock_specific_days} stock-spezifische Down-Days (20T)")
        elif stock_specific_days >= 1:
            score += 15 * stock_specific_days

        if own_ret_30d < -0.05 and peer_avg_30d < -0.03:
            score += min(40.0, abs(own_ret_30d) * 300)
            reasons.append(f"Sektor-Schwaeche: eigener {own_ret_30d:+.1%}, Peers {peer_avg_30d:+.1%}")
        elif delta_30d < -0.05:
            score += min(35.0, abs(delta_30d) * 250)
            reasons.append(f"Relative Schwaeche: {delta_30d:+.1%} vs Peers (30d)")

        if avg_corr < 0.3 and n_big_down >= 2:
            score += 10
            reasons.append(f"Niedrige Peer-Korrelation ({avg_corr:.2f}) — entkoppelt")

        score = min(100.0, score)
        triggered = score >= 35
        return DimensionScore(
            "peer_weakness", score, triggered,
            "; ".join(reasons) if reasons else f"im Peer-Bereich ({delta_30d:+.1%} vs Avg)",
            {"own_return_30d": float(own_ret_30d), "peer_avg_30d": peer_avg_30d,
             "delta_30d": round(delta_30d, 4), "stock_specific_days": stock_specific_days,
             "avg_peer_corr": round(avg_corr, 3), "peers": peers},
            weight=DIMENSION_WEIGHTS["peer_weakness"],
        )
    except Exception as e:
        return DimensionScore("peer_weakness", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["peer_weakness"])



# ──────────────── 8. VALUATION PERCENTILE ───────────────────
def score_valuation_percentile(ticker: str) -> DimensionScore:
    """P/E gegenüber eigener 5-J-Historie. > 90. Perzentil ist Warn-Signal."""
    try:
        fund = get_fundamentals(ticker)
        current_pe = fund.get("pe_ratio")
        if current_pe is None or current_pe <= 0:
            return DimensionScore("valuation_percentile", 0, False,
                                  "P/E nicht verfügbar", {},
                                  weight=DIMENSION_WEIGHTS["valuation_percentile"])

        prices = get_prices(ticker, period="5y")["close"]
        if len(prices) < 252:
            return DimensionScore("valuation_percentile", 0, False,
                                  "zu wenig Historie für Percentile", {},
                                  weight=DIMENSION_WEIGHTS["valuation_percentile"])

        current_price = float(prices.iloc[-1])
        percentile = float((prices < current_price).mean())

        score = 0.0
        reasons = []
        if percentile > 0.95:
            score = (percentile - 0.95) * 800  # 0-40 Punkte
            score += 25
            reasons.append(f"Kurs im {percentile:.0%}. Perzentil der 5-J-Historie")
        elif percentile > 0.90:
            score = (percentile - 0.90) * 200
            reasons.append(f"Kurs im {percentile:.0%}. Perzentil")

        if current_pe > 50:
            score += 15
            reasons.append(f"PE={current_pe:.0f} (hoch)")
        elif current_pe > 35:
            score += 5
            reasons.append(f"PE={current_pe:.0f}")

        score = min(100.0, score)
        triggered = score >= 45
        return DimensionScore(
            "valuation_percentile", score, triggered,
            "; ".join(reasons) if reasons else f"Perzentil {percentile:.0%} ok",
            {"current_pe": float(current_pe),
             "price_percentile": percentile},
            weight=DIMENSION_WEIGHTS["valuation_percentile"],
        )
    except Exception as e:
        return DimensionScore("valuation_percentile", 0, False,
                              f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["valuation_percentile"])


# ──────────────── 9. MACRO REGIME SHIFT ─────────────────────
def score_macro_regime() -> DimensionScore:
    """
    Macro-Regime-Score: kombiniert yfinance-VIX (Echtzeit-Spike-Detection)
    mit FRED Cross-Asset-Signalen (Yield-Curve, Credit-Spreads, Dollar, Inflation).

    Gewichtung: 50% VIX-Realtime + 50% FRED-Cross-Asset (max 100).
    """
    try:
        # ── Teil A: VIX via yfinance (Echtzeit-Spikes) ──────────
        vix_score = 0.0
        vix_reasons = []
        current_vix = 0.0
        vix_5d_change = 0.0
        vix_ok = True

        try:
            vix = get_prices("^VIX", period="3mo")
            current_vix = float(vix["close"].iloc[-1])
            vix_5d_change = float(vix["close"].iloc[-1] / vix["close"].iloc[-5] - 1)

            if current_vix > 30:
                vix_score += 45
                vix_reasons.append(f"VIX {current_vix:.1f} (stress)")
            elif current_vix > 20:
                vix_score += 25
                vix_reasons.append(f"VIX {current_vix:.1f} (elevated)")

            if vix_5d_change > 0.30:
                vix_score += 30
                vix_reasons.append(f"VIX +{vix_5d_change:.0%} in 5T")
            elif vix_5d_change > 0.15:
                vix_score += 15
                vix_reasons.append(f"VIX +{vix_5d_change:.0%} in 5T")
        except Exception:
            vix_ok = False

        # ── Teil B: FRED Cross-Asset-Signale ─────────────────────
        fred_ok = False
        fred_result = {"score": 0, "reasons": [], "details": {}}
        try:
            from .fred_signals import macro_risk_score
            fred_result = macro_risk_score()
            fred_ok = True
        except Exception:
            fred_ok = False

        # ── Teil C: Market Breadth ───────────────────────────────
        breadth_ok = False
        breadth_result = {"score": 0, "reasons": []}
        try:
            from .market_breadth import market_breadth_score
            breadth_result = market_breadth_score()
            breadth_ok = True
        except Exception:
            breadth_ok = False

        # ── Kombination: VIX 40% / FRED 35% / Breadth 25%, ABER auf die
        # VERFUEGBAREN Komponenten re-normalisiert. Ein Daten-Ausfall darf das
        # Macro-Risiko nicht still nach unten ziehen (sonst wirkt der Markt bei
        # Outage faelschlich ruhig -> System kauft aggressiver).
        vix_score = min(100.0, vix_score)
        fred_score = min(100.0, fred_result["score"])
        breadth_score = min(100.0, breadth_result.get("score", 0))
        _parts = []
        if vix_ok:     _parts.append((vix_score, 0.40))
        if fred_ok:    _parts.append((fred_score, 0.35))
        if breadth_ok: _parts.append((breadth_score, 0.25))
        if _parts:
            _tw = sum(w for _, w in _parts)
            score = sum(s * w for s, w in _parts) / _tw
        else:
            score = 0.0
        score = min(100.0, score)

        reasons = vix_reasons + fred_result.get("reasons", []) + breadth_result.get("reasons", [])
        triggered = score >= 35

        details = {
            "vix": current_vix,
            "vix_5d_change": vix_5d_change,
            "vix_sub_score": vix_score,
            "fred_sub_score": fred_score,
            "breadth_sub_score": breadth_score,
            "fred_details": fred_result.get("details", {}),
            "breadth_details": {k: v for k, v in breadth_result.items() if k not in ("score", "reasons")},
        }

        return DimensionScore(
            "macro_regime", score, triggered,
            "; ".join(reasons) if reasons else f"VIX {current_vix:.1f} ruhig",
            details,
            weight=DIMENSION_WEIGHTS["macro_regime"],
        )
    except Exception as e:
        return DimensionScore("macro_regime", 0, False,
                              f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["macro_regime"])


# ──────────────── 10. EARNINGS PROXIMITY ─────────────────────
def score_earnings_proximity(ticker: str) -> DimensionScore:
    """Erhoehtes Risiko rund um Earnings-Termine (Gap-Risk, IV-Crush, PEAD)."""
    try:
        from .earnings import compute_earnings_risk
        result = compute_earnings_risk(ticker)
        return DimensionScore(
            "earnings_proximity",
            result["score"],
            result["triggered"],
            result["reason"],
            {
                "next_earnings": result["next_earnings"],
                "days_until": result["days_until"],
                "days_since_last": result["days_since_last"],
            },
            weight=DIMENSION_WEIGHTS["earnings_proximity"],
        )
    except Exception as e:
        return DimensionScore("earnings_proximity", 0, False,
                              f"earnings error: {e}", {},
                              weight=DIMENSION_WEIGHTS["earnings_proximity"])




# ──────────────── 11. UP/DOWN VOLUME RATIO ──────────────────
def score_updown_volume(prices: pd.DataFrame) -> DimensionScore:
    """Up-Volume vs Down-Volume Ratio der letzten 20 Tage."""
    if len(prices) < 25:
        return DimensionScore("updown_volume", 0, False, "zu wenig Historie", {},
                              weight=DIMENSION_WEIGHTS["updown_volume"])
    recent = prices.tail(20).copy()
    daily_ret = recent["close"].pct_change()
    vol = recent["volume"]
    up_vol = float(vol[daily_ret > 0].sum())
    down_vol = float(vol[daily_ret < 0].sum())
    total = up_vol + down_vol
    if total == 0:
        return DimensionScore("updown_volume", 0, False, "kein Volumen", {},
                              weight=DIMENSION_WEIGHTS["updown_volume"])
    ratio = up_vol / down_vol if down_vol > 0 else 5.0
    down_pct = down_vol / total
    score = 0.0
    reasons = []
    if ratio < 0.6:
        score += 60 + 30 * (0.6 - ratio) / 0.6
        reasons.append(f"Up/Down-Ratio {ratio:.2f} — starke Distribution")
    elif ratio < 0.85:
        score += 35 + 25 * (0.85 - ratio) / 0.25
        reasons.append(f"Up/Down-Ratio {ratio:.2f} — leichte Distribution")
    elif ratio > 1.8:
        reasons.append(f"Up/Down-Ratio {ratio:.2f} — starke Akkumulation")
    down_streak = 0
    max_down_streak = 0
    for r in daily_ret.dropna():
        if r < 0:
            down_streak += 1
            max_down_streak = max(max_down_streak, down_streak)
        else:
            down_streak = 0
    if max_down_streak >= 4:
        score += 15
        reasons.append(f"{max_down_streak} Tage Down-Streak")
    score = min(100.0, score)
    return DimensionScore(
        "updown_volume", score, score >= 40,
        "; ".join(reasons) if reasons else "Volumen-Balance normal",
        {"up_down_ratio": round(ratio, 3), "down_vol_pct": round(down_pct, 3),
         "max_down_streak": max_down_streak},
        weight=DIMENSION_WEIGHTS["updown_volume"],
    )


# ──────────────── 12. HURST EXPONENT ───────────────────────
def _hurst_exponent(series: np.ndarray, max_lag: int = 40) -> float | None:
    """Rescaled Range (R/S) Hurst-Exponent."""
    n = len(series)
    if n < max_lag * 2:
        return None
    lags = range(10, max_lag + 1)
    rs_values = []
    for lag in lags:
        rs_lag = []
        for start in range(0, n - lag, lag):
            chunk = series[start:start + lag]
            mean_c = chunk.mean()
            devs = np.cumsum(chunk - mean_c)
            r = devs.max() - devs.min()
            s = chunk.std(ddof=1)
            if s > 0:
                rs_lag.append(r / s)
        if rs_lag:
            rs_values.append((np.log(lag), np.log(np.mean(rs_lag))))
    if len(rs_values) < 3:
        return None
    x = np.array([v[0] for v in rs_values])
    y = np.array([v[1] for v in rs_values])
    slope = float(np.polyfit(x, y, 1)[0])
    return max(0.0, min(1.0, slope))


def score_hurst_regime(prices: pd.DataFrame) -> DimensionScore:
    """Hurst Exponent: H<0.4=mean-reverting, H~0.5=random, H>0.6=trending."""
    if len(prices) < 100:
        return DimensionScore("hurst_regime", 0, False, "zu wenig Historie", {},
                              weight=DIMENSION_WEIGHTS["hurst_regime"])
    log_returns = np.log(prices["close"].values[1:] / prices["close"].values[:-1])
    log_returns = log_returns[~np.isnan(log_returns)]
    h = _hurst_exponent(log_returns)
    if h is None:
        return DimensionScore("hurst_regime", 0, False, "Hurst nicht berechenbar", {},
                              weight=DIMENSION_WEIGHTS["hurst_regime"])
    ret_20d = float(prices["close"].iloc[-1] / prices["close"].iloc[-20] - 1) if len(prices) >= 20 else 0.0
    score = 0.0
    reasons = []
    if h < 0.40:
        if ret_20d > 0.05:
            score = 55 + 30 * (0.40 - h) / 0.40
            reasons.append(f"H={h:.2f} mean-reverting nach +{ret_20d:.1%} Rallye — Reversal wahrscheinlich")
        elif ret_20d < -0.05:
            score = 20
            reasons.append(f"H={h:.2f} mean-reverting nach Dip — Bounce moeglich")
        else:
            score = 25
            reasons.append(f"H={h:.2f} mean-reverting — Range-gebunden")
    elif h > 0.60:
        if ret_20d < -0.03:
            score = 50 + 25 * (h - 0.60) / 0.40
            reasons.append(f"H={h:.2f} trending + Abwaertstrend {ret_20d:.1%} — Momentum-Sell")
        else:
            score = 10
            reasons.append(f"H={h:.2f} trending + Aufwaertstrend")
    else:
        score = 15
        reasons.append(f"H={h:.2f} — Random Walk")
    score = min(100.0, score)
    return DimensionScore(
        "hurst_regime", score, score >= 40,
        "; ".join(reasons),
        {"hurst": round(h, 3), "return_20d": round(ret_20d, 4),
         "regime": "mean_revert" if h < 0.4 else "trending" if h > 0.6 else "random"},
        weight=DIMENSION_WEIGHTS["hurst_regime"],
    )


# ──────────────── 13. VaR RISK ─────────────────────────────
def score_var_risk(prices: pd.DataFrame) -> DimensionScore:
    """Value-at-Risk (95%) und Conditional VaR. Hoher VaR = hohes Tail-Risiko."""
    if len(prices) < 60:
        return DimensionScore("var_risk", 0, False, "zu wenig Historie", {},
                              weight=DIMENSION_WEIGHTS["var_risk"])
    returns = prices["close"].pct_change().dropna().values
    recent_returns = returns[-60:]
    var_95 = float(np.percentile(recent_returns, 5))
    var_99 = float(np.percentile(recent_returns, 1))
    cvar_95 = float(recent_returns[recent_returns <= var_95].mean()) if (recent_returns <= var_95).any() else var_95
    vol_annual = float(np.std(recent_returns) * np.sqrt(252))
    score = 0.0
    reasons = []
    if var_95 < -0.04:
        score += 40 + 30 * min(1.0, (abs(var_95) - 0.04) / 0.06)
        reasons.append(f"VaR95 {var_95:.1%}/Tag — erhoehtes Tail-Risiko")
    elif var_95 < -0.025:
        score += 20 + 20 * (abs(var_95) - 0.025) / 0.015
        reasons.append(f"VaR95 {var_95:.1%}/Tag — moderate Tail-Risk")
    if cvar_95 < -0.06:
        score += 20
        reasons.append(f"CVaR95 {cvar_95:.1%} — schwere Tail-Events")
    if vol_annual > 0.50:
        score += 15
        reasons.append(f"Annual Vol {vol_annual:.0%} — extrem volatil")
    score = min(100.0, score)
    return DimensionScore(
        "var_risk", score, score >= 40,
        "; ".join(reasons) if reasons else f"VaR95 {var_95:.1%}, Vol {vol_annual:.0%} — normal",
        {"var_95": round(var_95, 4), "var_99": round(var_99, 4),
         "cvar_95": round(cvar_95, 4), "vol_annual": round(vol_annual, 4)},
        weight=DIMENSION_WEIGHTS["var_risk"],
    )


# ──────────────── 14. GAP ANALYSIS ─────────────────────────
def score_gap_pattern(prices: pd.DataFrame) -> DimensionScore:
    """Gap-Downs (Open vs prev Close). Haeufige Gap-Downs = Overnight-Selling-Pressure."""
    if len(prices) < 30:
        return DimensionScore("gap_pattern", 0, False, "zu wenig Historie", {},
                              weight=DIMENSION_WEIGHTS["gap_pattern"])
    recent = prices.tail(30)
    gaps = (recent["open"].values[1:] / recent["close"].values[:-1]) - 1
    gaps = gaps[~np.isnan(gaps)]
    if len(gaps) == 0:
        return DimensionScore("gap_pattern", 0, False, "keine Gap-Daten", {},
                              weight=DIMENSION_WEIGHTS["gap_pattern"])
    gap_downs = gaps[gaps < -0.01]
    n_gap_downs = len(gap_downs)
    avg_gap_down = float(gap_downs.mean()) if n_gap_downs > 0 else 0.0
    max_gap_down = float(gap_downs.min()) if n_gap_downs > 0 else 0.0
    intraday_returns = (recent["close"].values - recent["open"].values) / np.where(
        recent["open"].values != 0, recent["open"].values, 1.0)
    gap_fill_rate = 0.0
    if n_gap_downs > 0:
        filled = 0
        for i in range(1, len(recent)):
            if i - 1 < len(gaps) and gaps[i - 1] < -0.01:
                if i < len(intraday_returns) and intraday_returns[i] > abs(gaps[i - 1]) * 0.5:
                    filled += 1
        gap_fill_rate = filled / n_gap_downs
    score = 0.0
    reasons = []
    if n_gap_downs >= 7:
        score += 50 + 15 * min(1.0, (n_gap_downs - 7) / 5)
        reasons.append(f"{n_gap_downs} Gap-Downs >1% in 30T — persistente Overnight-Verkaeufe")
    elif n_gap_downs >= 4:
        score += 25 + 8 * (n_gap_downs - 4) / 3
        reasons.append(f"{n_gap_downs} Gap-Downs >1% in 30T")
    if max_gap_down < -0.05:
        score += 20
        reasons.append(f"Max Gap-Down {max_gap_down:.1%} — signifikant")
    elif max_gap_down < -0.03:
        score += 10
        reasons.append(f"Max Gap-Down {max_gap_down:.1%}")
    if gap_fill_rate < 0.3 and n_gap_downs >= 4:
        score += 15
        reasons.append(f"Gap-Fill-Rate nur {gap_fill_rate:.0%} — Gaps werden nicht gekauft")
    score = min(100.0, score)
    return DimensionScore(
        "gap_pattern", score, score >= 35,
        "; ".join(reasons) if reasons else "Gap-Muster unauffaellig",
        {"n_gap_downs_30d": int(n_gap_downs), "avg_gap_down": round(avg_gap_down, 4),
         "max_gap_down": round(max_gap_down, 4), "gap_fill_rate": round(gap_fill_rate, 3)},
        weight=DIMENSION_WEIGHTS["gap_pattern"],
    )


# ──────────────── 15. SHORT INTEREST ───────────────────────
def score_short_interest(ticker: str) -> DimensionScore:
    """Short Interest aus yfinance .info (shortPercentOfFloat, shortRatio)."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        return DimensionScore("short_interest", 0, False, f"yfinance error: {e}", {},
                              weight=DIMENSION_WEIGHTS["short_interest"])
    short_pct = info.get("shortPercentOfFloat")
    short_ratio = info.get("shortRatio")
    if short_pct is None and short_ratio is None:
        return DimensionScore("short_interest", 0, False,
                              "keine Short-Daten (ETF oder nicht verfuegbar)", {},
                              weight=DIMENSION_WEIGHTS["short_interest"])
    short_pct = float(short_pct or 0)
    short_ratio = float(short_ratio or 0)
    score = 0.0
    reasons = []
    if short_pct > 0.20:
        score += 50 + 25 * min(1.0, (short_pct - 0.20) / 0.20)
        reasons.append(f"Short Float {short_pct:.1%} — sehr hoch")
    elif short_pct > 0.10:
        score += 30 + 20 * (short_pct - 0.10) / 0.10
        reasons.append(f"Short Float {short_pct:.1%} — erhoeht")
    elif short_pct > 0.05:
        score += 10 + 10 * (short_pct - 0.05) / 0.05
        reasons.append(f"Short Float {short_pct:.1%}")
    if short_ratio > 5:
        score += 20
        reasons.append(f"Days to Cover {short_ratio:.1f} — Squeeze-Potenzial")
    elif short_ratio > 3:
        score += 10
        reasons.append(f"Days to Cover {short_ratio:.1f}")
    score = min(100.0, score)
    return DimensionScore(
        "short_interest", score, score >= 35,
        "; ".join(reasons) if reasons else f"Short Float {short_pct:.1%} — normal",
        {"short_pct_float": round(short_pct, 4), "short_ratio": round(short_ratio, 2)},
        weight=DIMENSION_WEIGHTS["short_interest"],
    )



# ──────────────── 17. CROSS-ASSET CORRELATION ──────────────
CROSS_ASSETS = {
    "BTC-USD":  "Risk-On Proxy (Crypto)",
    "TLT":      "Long Bonds (Flight-to-Safety)",
    "GLD":      "Gold (Inflation-Hedge)",
    "UUP":      "US Dollar Index",
    "^VIX":     "Volatility Index",
}


def score_cross_asset(ticker: str) -> DimensionScore:
    """Korrelation mit Cross-Asset-Indikatoren (BTC, Bonds, Gold, Dollar, VIX)."""
    try:
        own = get_prices(ticker, period="3mo")
        if len(own) < 30:
            return DimensionScore("cross_asset", 0, False, "zu wenig Historie", {},
                                  weight=DIMENSION_WEIGHTS["cross_asset"])
        own_ret = own["close"].pct_change().dropna().tail(20)
        own_ret.index = own_ret.index.tz_localize(None) if own_ret.index.tz else own_ret.index
        correlations = {}
        for asset, desc in CROSS_ASSETS.items():
            try:
                adf = get_prices(asset, period="3mo")
                aret = adf["close"].pct_change().dropna().tail(20)
                aret.index = aret.index.tz_localize(None) if aret.index.tz else aret.index
                merged = pd.DataFrame({"own": own_ret, "asset": aret}).dropna()
                if len(merged) >= 10:
                    c = float(merged["own"].corr(merged["asset"]))
                    if not np.isnan(c):
                        correlations[asset] = round(c, 3)
            except Exception:
                continue
        if not correlations:
            return DimensionScore("cross_asset", 0, False, "keine Cross-Asset-Daten", {},
                                  weight=DIMENSION_WEIGHTS["cross_asset"])
        score = 0.0
        reasons = []
        vix_corr = correlations.get("^VIX", 0)
        tlt_corr = correlations.get("TLT", 0)
        btc_corr = correlations.get("BTC-USD", 0)
        gld_corr = correlations.get("GLD", 0)
        if vix_corr > 0.3:
            score += 25 + 15 * min(1.0, (vix_corr - 0.3) / 0.4)
            reasons.append(f"VIX-Korr +{vix_corr:.2f} — bewegt sich mit Angst")
        if tlt_corr < -0.3:
            score += 20
            reasons.append(f"TLT-Korr {tlt_corr:.2f} — leidet bei Flight-to-Safety")
        if btc_corr > 0.5:
            score += 15
            reasons.append(f"BTC-Korr +{btc_corr:.2f} — Risk-On-Trade")
        if gld_corr > 0.4 and ticker not in ("GLD", "GDX", "XLE", "XLB"):
            score += 10
            reasons.append(f"Gold-Korr +{gld_corr:.2f} — Regime-Shift?")
        abs_corrs = [abs(v) for v in correlations.values()]
        if len(abs_corrs) >= 3 and np.mean(abs_corrs) > 0.5:
            score += 15
            reasons.append(f"Avg |Korr| {np.mean(abs_corrs):.2f} — alles korreliert, Crash-Regime")
        score = min(100.0, score)
        return DimensionScore(
            "cross_asset", score, score >= 35,
            "; ".join(reasons) if reasons else "Cross-Asset-Muster unauffaellig",
            correlations,
            weight=DIMENSION_WEIGHTS["cross_asset"],
        )
    except Exception as e:
        return DimensionScore("cross_asset", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["cross_asset"])


# ──────────────── 18. SHORT INTEREST TREND ─────────────────
def score_si_trend(ticker: str) -> DimensionScore:
    """Short Interest Trend aus historisch gespeicherten Werten in risk_scores DB."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        current_si = info.get("shortPercentOfFloat")
        if current_si is None:
            return DimensionScore("si_trend", 0, False, "keine Short-Daten", {},
                                  weight=DIMENSION_WEIGHTS["si_trend"])
        current_si = float(current_si)
        from ..common.storage import ALERTS_DB, connect
        from ..common.json_utils import safe_parse
        rows = []
        try:
            with connect(ALERTS_DB) as conn:
                rows = conn.execute(
                    """SELECT timestamp, dimensions_js FROM risk_scores
                       WHERE ticker=? ORDER BY timestamp DESC LIMIT 100""",
                    (ticker,)).fetchall()
        except Exception:
            pass
        historical_si = []
        for row in rows:
            dims = safe_parse(row["dimensions_js"] or "[]", default=[])
            for d in dims:
                if d.get("name") == "short_interest":
                    si_val = d.get("evidence", {}).get("short_pct_float")
                    if si_val is not None:
                        historical_si.append(float(si_val))
        score = 0.0
        reasons = []
        if len(historical_si) >= 5:
            oldest_si = float(np.mean(historical_si[-5:]))
            si_change = current_si - oldest_si
            if si_change > 0.03:
                score += 40 + 20 * min(1.0, (si_change - 0.03) / 0.07)
                reasons.append(f"SI steigt: {oldest_si:.1%} -> {current_si:.1%}")
            elif si_change > 0.01:
                score += 20 + 10 * (si_change - 0.01) / 0.02
                reasons.append(f"SI leicht steigend: +{si_change:.1%}")
            elif si_change < -0.03:
                reasons.append(f"SI faellt: Shorts covern")
        else:
            reasons.append(f"SI={current_si:.1%}, zu wenig Historie fuer Trend")
            if current_si > 0.15:
                score += 20
        score = min(100.0, score)
        return DimensionScore(
            "si_trend", score, score >= 30,
            "; ".join(reasons) if reasons else f"SI-Trend stabil ({current_si:.1%})",
            {"current_si": round(current_si, 4), "n_historical": len(historical_si),
             "oldest_si": round(float(np.mean(historical_si[-5:])), 4) if len(historical_si) >= 5 else None},
            weight=DIMENSION_WEIGHTS["si_trend"],
        )
    except Exception as e:
        return DimensionScore("si_trend", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["si_trend"])


# ──────────────── 19. EARNINGS HEADLINE HEURISTIC ──────────
_BEARISH_KW = ("miss", "missed", "lowered", "guidance down", "cut", "warns",
               "disappoints", "decline", "weak", "loss", "below expectations",
               "downgrade", "slump", "plunge", "shortfall")
_BULLISH_KW = ("beat", "beats", "raised", "guidance up", "record", "surpass",
               "strong", "exceeds", "upside", "upgrade", "surge", "growth")


def score_earnings_llm(ticker: str) -> DimensionScore:
    """Keyword-basierte Earnings-Headline-Analyse (ersetzt LLM-Version)."""
    try:
        from .earnings import get_last_earnings_date
        last_ed = get_last_earnings_date(ticker)
        if not last_ed or (dt.date.today() - last_ed).days > 30:
            return DimensionScore("earnings_llm", 0, False,
                                  "keine kuerzlichen Earnings (>30d)",
                                  {"last_earnings": str(last_ed) if last_ed else None},
                                  weight=DIMENSION_WEIGHTS["earnings_llm"])

        headlines = []
        try:
            import yfinance as yf
            news = yf.Ticker(ticker).news or []
            for n in news[:15]:
                t = n.get("title", "")
                if t:
                    headlines.append(t)
        except Exception:
            pass

        if not headlines:
            return DimensionScore("earnings_llm", 0, False,
                                  "keine Headlines verfuegbar",
                                  {"last_earnings": str(last_ed)},
                                  weight=DIMENSION_WEIGHTS["earnings_llm"])

        bear_hits = 0
        bull_hits = 0
        for h in headlines:
            hl = h.lower()
            if any(kw in hl for kw in _BEARISH_KW):
                bear_hits += 1
            if any(kw in hl for kw in _BULLISH_KW):
                bull_hits += 1

        net = bear_hits - bull_hits
        score = min(100.0, max(0.0, 20.0 + net * 15.0)) if net > 0 else 0.0

        guidance = "lowered" if net >= 2 else ("raised" if net <= -2 else "mixed")
        triggered = score >= 40

        return DimensionScore(
            "earnings_llm", score, triggered,
            f"Headline-Heuristik: {bear_hits} bearish, {bull_hits} bullish, guidance={guidance}",
            {"bear_hits": bear_hits, "bull_hits": bull_hits, "net": net,
             "guidance": guidance, "n_headlines": len(headlines),
             "last_earnings": str(last_ed), "cost_eur": 0.0},
            weight=DIMENSION_WEIGHTS["earnings_llm"],
        )
    except Exception as e:
        return DimensionScore("earnings_llm", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["earnings_llm"])


# ──────────────── 20. GOOGLE TRENDS ──────────────────────
_TRENDS_CACHE: dict[str, tuple[float, float]] = {}  # ticker -> (timestamp, score)
_TRENDS_CACHE_TTL = 3600 * 6  # 6h cache


def score_google_trends(ticker: str) -> DimensionScore:
    """
    Misst oeffentliches Suchinteresse via Google Trends.
    Spike im Suchvolumen + fallender Kurs = Retail-Panik-Signal.
    """
    import time as _time
    cached = _TRENDS_CACHE.get(ticker)
    if cached and (_time.time() - cached[0]) < _TRENDS_CACHE_TTL:
        score_val = cached[1]
        return DimensionScore(
            "google_trends", score_val, score_val >= 35,
            f"cached trend score={score_val:.0f}",
            {"cached": True},
            weight=DIMENSION_WEIGHTS["google_trends"],
        )

    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(5, 10))
        kw = [f"{ticker} stock"]
        pytrends.build_payload(kw, cat=0, timeframe="now 7-d", geo="US")
        df = pytrends.interest_over_time()

        if df is None or df.empty or f"{ticker} stock" not in df.columns:
            _TRENDS_CACHE[ticker] = (_time.time(), 0.0)
            return DimensionScore("google_trends", 0, False,
                                  "keine Trends-Daten", {"available": False},
                                  weight=DIMENSION_WEIGHTS["google_trends"])

        vals = df[f"{ticker} stock"].values
        if len(vals) < 10:
            _TRENDS_CACHE[ticker] = (_time.time(), 0.0)
            return DimensionScore("google_trends", 0, False,
                                  "zu wenig Datenpunkte", {"n_points": len(vals)},
                                  weight=DIMENSION_WEIGHTS["google_trends"])

        recent = float(np.mean(vals[-12:]))
        baseline = float(np.mean(vals[:-12])) if len(vals) > 12 else float(np.mean(vals))
        peak = float(np.max(vals))
        ratio = recent / baseline if baseline > 0 else 1.0

        prices = get_prices(ticker, period="5d")
        price_falling = False
        if prices is not None and len(prices) >= 2:
            ret_5d = (prices["close"].iloc[-1] / prices["close"].iloc[0]) - 1
            price_falling = ret_5d < -0.02

        if ratio >= 2.0 and price_falling:
            score_val = min(85.0, 40 + (ratio - 1) * 20)
        elif ratio >= 1.5 and price_falling:
            score_val = min(60.0, 25 + (ratio - 1) * 15)
        elif ratio >= 2.0:
            score_val = min(40.0, 15 + (ratio - 1) * 10)
        else:
            score_val = max(0.0, (ratio - 1) * 15)

        triggered = score_val >= 35
        _TRENDS_CACHE[ticker] = (_time.time(), score_val)

        return DimensionScore(
            "google_trends", round(score_val, 1), triggered,
            f"trend ratio={ratio:.1f}x, recent={recent:.0f}, base={baseline:.0f}, "
            f"{'Kurs faellt' if price_falling else 'Kurs stabil'}",
            {"ratio": round(ratio, 2), "recent": round(recent, 1),
             "baseline": round(baseline, 1), "peak": round(peak, 1),
             "price_falling": price_falling},
            weight=DIMENSION_WEIGHTS["google_trends"],
        )
    except Exception as e:
        _TRENDS_CACHE[ticker] = (_time.time(), 0.0)
        return DimensionScore("google_trends", 0, False, f"error: {e}", {},
                              weight=DIMENSION_WEIGHTS["google_trends"])


# ──────────────── 16. SIGNAL COHERENCE HEURISTIC ──────────
_COHERENCE_GROUPS = {
    "technical":   {"technical_breakdown", "volume_divergence", "updown_volume", "gap_pattern", "hurst_regime"},
    "fundamental": {"valuation_percentile", "analyst_downgrades", "earnings_llm"},
    "sentiment":   {"sentiment_reversal", "short_interest", "si_trend", "options_skew", "google_trends"},
    "macro":       {"macro_regime", "cross_asset", "earnings_proximity"},
}


def score_llm_context(ticker: str, dimensions: list[DimensionScore]) -> DimensionScore:
    """Heuristischer Kohaerenz-Check (ersetzt LLM-Meta-Analyse)."""
    triggered_dims = [d for d in dimensions if d.triggered]
    n_triggered = len(triggered_dims)
    if n_triggered < 3:
        return DimensionScore("llm_context", 0, False,
                              "zu wenig aktive Signale fuer Kohaerenz-Check",
                              {"skipped": True, "triggered_n": n_triggered},
                              weight=DIMENSION_WEIGHTS["llm_context"])

    try:
        triggered_names = {d.name for d in triggered_dims}
        active_groups = set()
        for group, members in _COHERENCE_GROUPS.items():
            if triggered_names & members:
                active_groups.add(group)

        n_groups = len(active_groups)
        avg_triggered_score = sum(d.score for d in triggered_dims) / n_triggered

        coherent = n_groups >= 2
        if n_groups >= 3:
            score = min(100.0, avg_triggered_score * 1.1)
        elif n_groups == 2:
            score = avg_triggered_score * 0.85
        else:
            score = avg_triggered_score * 0.5

        score = min(100.0, max(0.0, score))
        triggered = score >= 40 and coherent

        reason_parts = []
        if coherent:
            reason_parts.append(f"Kohaerentes Risiko: {n_groups} Kategorien aktiv (Score {score:.0f})")
        else:
            reason_parts.append(f"Isoliertes Signal: nur {n_groups} Kategorie (Score {score:.0f})")
        grp_str = ", ".join(sorted(active_groups))
        reason_parts.append(f"Gruppen: {grp_str}")

        return DimensionScore(
            "llm_context", score, triggered,
            "; ".join(reason_parts),
            {"n_groups": n_groups, "active_groups": sorted(active_groups),
             "coherent": coherent, "avg_triggered_score": round(avg_triggered_score, 1),
             "n_triggered": n_triggered, "cost_eur": 0.0},
            weight=DIMENSION_WEIGHTS["llm_context"],
        )
    except Exception as e:
        return DimensionScore("llm_context", 0, False,
                              f"coherence error: {e}", {},
                              weight=DIMENSION_WEIGHTS["llm_context"])


# ════════════════════════════════════════════════════════════
#  COMPOSITE SCORING
# ════════════════════════════════════════════════════════════
def score_ticker(
    ticker: str,
    finnhub_key: Optional[str] = None,
    news_api_key: Optional[str] = None,
    learning_context: Optional[str] = None,
) -> RiskReport:
    """Berechne Composite-Risk-Score für einen einzelnen Ticker."""
    print(f"\n🔍 Scoring {ticker}…")
    prices = get_prices(ticker, period="2y")

    dimensions = [
        score_technical_breakdown(prices),
        score_volume_divergence(prices),
        score_insider_selling(ticker, finnhub_key),
        score_analyst_downgrades(ticker, finnhub_key),
        score_options_skew(ticker),
        score_sentiment_reversal(ticker, news_api_key),
        score_peer_weakness(ticker),
        score_valuation_percentile(ticker),
        score_macro_regime(),
        score_earnings_proximity(ticker),
        score_updown_volume(prices),
        score_hurst_regime(prices),
        score_var_risk(prices),
        score_gap_pattern(prices),
        score_short_interest(ticker),
    ]

    # Cross-Asset + SI Trend + Earnings LLM
    dimensions.append(score_cross_asset(ticker))
    dimensions.append(score_si_trend(ticker))
    dimensions.append(score_earnings_llm(ticker))
    dimensions.append(score_google_trends(ticker))

    # LLM Context Analysis: Meta-Layer ueber alle anderen Dimensionen (last)
    dimensions.append(score_llm_context(ticker, dimensions))

    # Composite: gewichteter Durchschnitt der getriggerten Dimensionen,
    # skaliert mit Breadth-Faktor. Bei 0 Triggers: gedaempfter All-Dims-Average.
    triggered_dims = [d for d in dimensions if d.triggered]
    n_triggered = len(triggered_dims)

    if n_triggered >= 1:
        total_weight = sum(d.weight for d in triggered_dims)
        weighted_sum = sum(d.score * d.weight for d in triggered_dims)
        avg_score = weighted_sum / total_weight if total_weight > 0 else 0
        breadth = min(1.0, 0.5 + 0.1 * n_triggered)
        composite = avg_score * breadth
    else:
        all_weight = sum(d.weight for d in dimensions)
        all_sum = sum(d.score * d.weight for d in dimensions)
        composite = (all_sum / all_weight * 0.25) if all_weight > 0 else 0
    # Regime-aware Dampening: im Bull-Market sind Risk-Scores systematisch
    # ueberhoeht weil die meisten Dimensionen kontraer wirken
    try:
        from ..learning.regime import current_regime
        _regime = current_regime()
        if _regime.label == "low_vol_bull" and _regime.probability >= 0.60:
            composite *= 0.70
        elif _regime.label == "bear" and _regime.probability >= 0.55:
            composite *= 1.15
            composite = min(100.0, composite)
    except Exception:
        pass

    alert_level = _alert_level_from_score(composite)
    alert_label = {0: "Green", 1: "Watch", 2: "Caution", 3: "Red"}[alert_level]

    report = RiskReport(
        ticker=ticker,
        timestamp=dt.datetime.now().isoformat(timespec="seconds"),
        composite=round(composite, 1),
        alert_level=alert_level,
        alert_label=alert_label,
        dimensions=dimensions,
    )

    # ── Self-Learning-Loop: jede Score-Berechnung als prediction-Row ──────
    n_stubs = sum(1 for d in dimensions if d.evidence.get("todo") is not None)
    confidence = (
        "high"   if n_stubs == 0 and report.triggered_count >= 3
        else "medium" if n_stubs <= 2
        else "low"
    )
    # Historische Analoga (Pattern-Library)
    analogs = []
    try:
        features = compute_features(prices, len(prices) - 1)
        if features is not None:
            matches = find_similar_patterns(features, lookback_days=7, top_k=3)
            analogs = [
                {
                    "ticker":         m["ticker"],
                    "peak_date":      m["peak_date"],
                    "drawdown_pct":   m["drawdown_pct"],
                    "days_to_trough": m["days_to_trough"],
                    "regime":         m["regime"],
                    "recovery_days":  m["recovery_days"],
                    "distance":       m["distance"],
                }
                for m in matches
            ]
    except Exception:
        pass

    _prompt_desc = "risk_scorer.score_ticker / 19-dim heuristic+llm+cross-asset+earnings / weights-v5 / pattern-augmented"
    if learning_context:
        _prompt_desc += f"\n\n--- LEARNING CONTEXT ---\n{learning_context}"

    pred_id = log_prediction(
        job_source="daily_score",
        model="heuristic-v5",
        subject_type="ticker",
        subject_id=ticker,
        prompt=_prompt_desc,
        input_payload={
            "ticker": ticker,
            "n_dimensions": len(dimensions),
            "stubs": n_stubs,
            "weights_version": "v1",
            "n_analogs": len(analogs),
        },
        input_summary=f"{ticker}, {len(dimensions)} dims, {n_stubs} stubs, {len(analogs)} analogs",
        output={
            "composite":        report.composite,
            "alert_level":      report.alert_level,
            "alert_label":      report.alert_label,
            "triggered_n":      report.triggered_count,
            "triggered_dims":   report.triggered_dimensions,
            "dimensions":       [asdict(d) for d in dimensions],
            "analogs":          analogs,
        },
        confidence=confidence,
        cost_estimate_eur=0.0,
    )
    _persist(report, prediction_id=pred_id)

    # ── Regime-Snapshot: welches Regime war bei dieser Prediction aktiv? ──
    try:
        from ..learning.regime_tracker import snap_regime
        snap_regime(prediction_id=pred_id)
    except Exception:
        pass

    return report


def _alert_level_from_score(score: float) -> int:
    for level, (lo, hi) in ALERT_THRESHOLDS.items():
        if lo <= score < hi:
            return level
    return 0


def _persist(report: RiskReport, prediction_id: Optional[int] = None) -> None:
    dims_json = json.dumps([asdict(d) for d in report.dimensions], default=str)
    with connect(ALERTS_DB) as conn:
        conn.execute(
            """
            INSERT INTO risk_scores
                (ticker, timestamp, composite, alert_level,
                 triggered_n, dimensions_js, prediction_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (report.ticker, report.timestamp, report.composite,
             report.alert_level, report.triggered_count, dims_json,
             prediction_id),
        )


# ════════════════════════════════════════════════════════════
#  PRETTY PRINT
# ════════════════════════════════════════════════════════════
def print_report(report: RiskReport) -> None:
    """Konsolen-Ausgabe eines Risk-Reports in hübsch."""
    color = {0: "\033[32m", 1: "\033[33m", 2: "\033[38;5;208m", 3: "\033[31m"}
    reset = "\033[0m"

    print()
    print("=" * 62)
    print(f" {report.ticker}  ·  Risk Report  ·  {report.timestamp}")
    print("=" * 62)
    print(f" Composite Score: {report.composite:5.1f} / 100")
    print(f" Alert Level:     {color[report.alert_level]}{report.alert_level} · {report.alert_label}{reset}")
    print(f" Aktive Signale:  {report.triggered_count} / {len(report.dimensions)}")
    print("-" * 62)

    for d in report.dimensions:
        mark = "⚠ " if d.triggered else "  "
        name = d.name.replace("_", " ").title()
        stub_flag = " [STUB]" if d.evidence.get("todo") else ""
        print(f" {mark}{name:<26}{stub_flag:<8} {d.score:5.1f}  ")
        if d.reason and d.reason != "keine Schwäche":
            print(f"        ↳ {d.reason}")
    print("=" * 62)

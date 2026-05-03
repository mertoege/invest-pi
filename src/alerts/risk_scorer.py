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
    0: (0,   25),     # Green  · Normal
    1: (25,  50),     # Watch  · Beobachten
    2: (50,  75),     # Caution · Vorsicht
    3: (75, 101),     # Red    · Handlung
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
    if current < ma50:
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

    score = min(100.0, score)
    triggered = score >= 40
    return DimensionScore(
        "technical_breakdown", score, triggered,
        "; ".join(reasons) if reasons else "keine Schwäche",
        {"current": float(current), "ma50": ma50, "ma200": ma200,
         "macd": float(macd)},
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
    Finnhub /stock/insider-transactions: Form-4-Daten der letzten 90 Tage.
    Scoring:
      - Zaehle Sell-Transaktionen der letzten 30 Tage
      - >= 3 verschiedene Insider verkaufen → triggered
      - Score proportional zu Anzahl Seller
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

        cutoff = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        recent_sells = [t for t in data
                        if t.get("transactionType") in ("S - Sale", "S - Sale+OE")
                        and (t.get("filingDate", "") >= cutoff)]

        unique_sellers = len(set(t.get("name", "") for t in recent_sells))
        total_shares_sold = sum(abs(t.get("share", 0)) for t in recent_sells)

        score = 0.0
        reasons = []
        if unique_sellers >= 5:
            score = min(80.0, unique_sellers * 12)
            reasons.append(f"{unique_sellers} Insider verkauft (30d)")
        elif unique_sellers >= 3:
            score = unique_sellers * 10
            reasons.append(f"{unique_sellers} Insider verkauft (30d)")
        elif unique_sellers >= 1:
            score = unique_sellers * 5
            reasons.append(f"{unique_sellers} Insider verkauft (30d)")

        triggered = score >= 30
        return DimensionScore(
            "insider_selling", min(100, score), triggered,
            "; ".join(reasons) if reasons else "keine auffälligen Insider-Verkäufe",
            {"unique_sellers_30d": unique_sellers, "total_shares_sold": total_shares_sold,
             "transactions_90d": len(data)},
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
    Analysiere die Options-Chain: ist der Put/Call-Ratio ungewöhnlich hoch?
    Funktioniert mit yfinance-Optionsdaten (15 min delayed, frei).
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        expirations = tk.options
        if not expirations:
            return DimensionScore("options_skew", 0, False,
                                  "keine Options verfügbar", {},
                                  weight=DIMENSION_WEIGHTS["options_skew"])

        # Nähester Verfallstermin
        nearest = expirations[0]
        chain = tk.option_chain(nearest)

        call_oi = chain.calls["openInterest"].sum()
        put_oi  = chain.puts["openInterest"].sum()
        if call_oi == 0:
            return DimensionScore("options_skew", 0, False, "kein Call-OI", {},
                                  weight=DIMENSION_WEIGHTS["options_skew"])

        pc_ratio = put_oi / call_oi

        # Schwellen: historischer Schnitt ~0.7, Extrem > 1.5
        score = 0.0
        reasons = []
        if pc_ratio > 1.5:
            score = min(100.0, (pc_ratio - 1.0) * 50)
            reasons.append(f"Put/Call Ratio {pc_ratio:.2f} (hoch)")
        elif pc_ratio > 1.0:
            score = (pc_ratio - 0.7) * 40
            reasons.append(f"Put/Call Ratio {pc_ratio:.2f} (erhöht)")

        triggered = score >= 40
        return DimensionScore(
            "options_skew", score, triggered,
            "; ".join(reasons) if reasons else f"P/C-Ratio {pc_ratio:.2f} normal",
            {"put_call_ratio": float(pc_ratio),
             "call_oi": int(call_oi), "put_oi": int(put_oi),
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
}


def score_peer_weakness(ticker: str) -> DimensionScore:
    """Vergleiche relative Performance der letzten 30 Tage vs. Peers."""
    peers = PEER_MAP.get(ticker)
    if not peers:
        return DimensionScore("peer_weakness", 0, False,
                              f"keine Peer-Definition für {ticker}", {},
                              weight=DIMENSION_WEIGHTS["peer_weakness"])

    try:
        own = get_prices(ticker, period="3mo")
        own_ret = (own["close"].iloc[-1] / own["close"].iloc[-30]) - 1

        peer_rets = []
        for p in peers:
            try:
                peer_df = get_prices(p, period="3mo")
                peer_rets.append((peer_df["close"].iloc[-1] / peer_df["close"].iloc[-30]) - 1)
            except Exception:
                continue

        if not peer_rets:
            return DimensionScore("peer_weakness", 0, False,
                                  "keine Peer-Daten", {},
                                  weight=DIMENSION_WEIGHTS["peer_weakness"])

        peer_avg = float(np.mean(peer_rets))
        delta = own_ret - peer_avg

        # Score: wie weit ist unser Titel hinter den Peers zurück?
        score = 0.0
        reasons = []

        # Fall A: eigener Titel fällt, Peers fallen auch → Sektor-Schwäche
        if own_ret < -0.05 and peer_avg < -0.03:
            score = min(100.0, abs(own_ret) * 500)
            reasons.append(f"Sektor-Schwäche: eigener {own_ret:+.1%}, Peers {peer_avg:+.1%}")
        # Fall B: eigener Titel schwächer als Peers → relative Schwäche
        elif delta < -0.05:
            score = min(100.0, abs(delta) * 300)
            reasons.append(f"Unter Peer-Durchschnitt: {delta:+.1%} relativ")

        triggered = score >= 40
        return DimensionScore(
            "peer_weakness", score, triggered,
            "; ".join(reasons) if reasons else f"im Peer-Bereich ({delta:+.1%} vs Avg)",
            {"own_return_30d": float(own_ret),
             "peer_avg": peer_avg,
             "peers": peers},
            weight=DIMENSION_WEIGHTS["peer_weakness"],
        )
    except Exception as e:
        return DimensionScore("peer_weakness", 0, False,
                              f"error: {e}", {},
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
        if percentile > 0.90:
            score = (percentile - 0.90) * 500  # 0-50 Punkte
            score += 20  # Grund-Malus
            reasons.append(f"Kurs im {percentile:.0%}. Perzentil der 5-J-Historie")
        elif percentile > 0.80:
            score = (percentile - 0.80) * 200
            reasons.append(f"Kurs im {percentile:.0%}. Perzentil")

        score = min(100.0, score)
        triggered = score >= 40
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
            pass

        # ── Teil B: FRED Cross-Asset-Signale ─────────────────────
        fred_result = {"score": 0, "reasons": [], "details": {}}
        try:
            from .fred_signals import macro_risk_score
            fred_result = macro_risk_score()
        except Exception:
            pass

        # ── Teil C: Market Breadth ───────────────────────────────
        breadth_result = {"score": 0, "reasons": []}
        try:
            from .market_breadth import market_breadth_score
            breadth_result = market_breadth_score()
        except Exception:
            pass

        # ── Kombination: 40% VIX + 35% FRED + 25% Breadth ───────
        vix_score = min(100.0, vix_score)
        fred_score = min(100.0, fred_result["score"])
        breadth_score = min(100.0, breadth_result.get("score", 0))
        score = (vix_score * 0.40) + (fred_score * 0.35) + (breadth_score * 0.25)
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
    ]

    # Composite: gewichteter Durchschnitt, bei dem nicht-implementierte Stubs
    # (score=0, evidence enthält "todo") mit reduziertem Einfluss eingehen
    total_weight = 0.0
    weighted_sum = 0.0
    for d in dimensions:
        is_stub = d.evidence.get("todo") is not None
        effective_weight = d.weight * (0.3 if is_stub else 1.0)
        weighted_sum += d.score * effective_weight
        total_weight += effective_weight

    composite = weighted_sum / total_weight if total_weight > 0 else 0
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

    _prompt_desc = "risk_scorer.score_ticker / 9-dim heuristic / weights-v1 / pattern-augmented"
    if learning_context:
        _prompt_desc += f"\n\n--- LEARNING CONTEXT ---\n{learning_context}"

    pred_id = log_prediction(
        job_source="daily_score",
        model="heuristic-v1",
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

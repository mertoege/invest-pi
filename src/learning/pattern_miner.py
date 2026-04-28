"""
Pattern Miner — § 03 Historical Learning

Extrahiert aus historischen Kursdaten Pre-Drawdown-Muster:
  1. Findet alle Drawdowns > threshold (default 15 %)
  2. Berechnet für jeden Drawdown den Zustand (Feature-Vektor) davor
  3. Speichert alles in patterns.db

Später kann das System den aktuellen Zustand mit der Library vergleichen
und die K ähnlichsten historischen Muster + deren Outcome zurückgeben.

WICHTIG — ehrliche Framing:
  Diese Library ist KEIN trainiertes Vorhersage-Modell. Sie ist eine
  Ähnlichkeits-Datenbank. Wenn das System sagt "aktuelle Lage ähnelt 2018
  Q3", heißt das: "5 der 7 Feature-Dimensionen matchen" — NICHT "NVDA fällt
  jetzt garantiert 50 %". Marktmuster wiederholen sich mit Variation,
  nicht identisch.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from ..common.data_loader import get_prices
from ..common.storage import PATTERNS_DB, connect


# ────────────────────────────────────────────────────────────
# KONFIGURATION
# ────────────────────────────────────────────────────────────
DRAWDOWN_THRESHOLD = 0.15        # Nur Drawdowns > 15 % beachten
MIN_RECOVERY_DAYS  = 10          # Mindest-Erholung zwischen Drawdown-Events
LOOKBACKS_DAYS     = [1, 7, 30]  # Feature-Snapshots X Tage VOR dem Peak


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class DrawdownEvent:
    ticker:         str
    peak_date:      str
    peak_price:     float
    trough_date:    str
    trough_price:   float
    drawdown_pct:   float
    days_to_trough: int
    recovery_days:  Optional[int]
    regime:         str           # bull_correction / bear_market / black_swan


@dataclass
class FeatureVector:
    """Zustand zu einem bestimmten Zeitpunkt, normalisiert für Similarity Search."""
    ret_30d:          float
    ret_90d:          float
    ret_180d:         float
    volatility_30d:   float
    rsi_14:           float
    price_vs_ma50:    float
    price_vs_ma200:   float
    volume_trend_30d: float
    drawdown_prior_y: float

    def to_array(self) -> np.ndarray:
        """Als numpy array, Reihenfolge stabil für distance-Berechnung."""
        return np.array([
            self.ret_30d, self.ret_90d, self.ret_180d,
            self.volatility_30d, self.rsi_14,
            self.price_vs_ma50, self.price_vs_ma200,
            self.volume_trend_30d, self.drawdown_prior_y,
        ])


# ────────────────────────────────────────────────────────────
# DRAWDOWN DETECTION
# ────────────────────────────────────────────────────────────
def detect_drawdowns(
    prices: pd.DataFrame,
    threshold: float = DRAWDOWN_THRESHOLD,
) -> list[DrawdownEvent]:
    """
    Identifiziert alle Drawdown-Events > threshold in der Kurshistorie.

    Algorithmus:
      - iteriere durch Kurse, tracke running peak
      - wenn close < running_peak * (1 - threshold): wir sind in Drawdown
      - finde den tiefsten Punkt nach Peak
      - wenn close wieder zurück zum Peak: Drawdown geschlossen
      - speichere Event mit peak/trough/recovery-Infos
    """
    close = prices["close"].values
    dates = prices.index

    events: list[DrawdownEvent] = []
    peak_idx = 0
    peak_price = close[0]
    trough_idx: Optional[int] = None
    trough_price: Optional[float] = None
    in_drawdown = False

    for i in range(1, len(close)):
        price = close[i]

        if price > peak_price:
            # Neues Hoch erreicht
            if in_drawdown and trough_idx is not None:
                # Drawdown-Event abschließen
                drawdown_pct = (trough_price - peak_price) / peak_price
                events.append(DrawdownEvent(
                    ticker="",  # wird nachgetragen
                    peak_date=str(dates[peak_idx].date()),
                    peak_price=float(peak_price),
                    trough_date=str(dates[trough_idx].date()),
                    trough_price=float(trough_price),
                    drawdown_pct=float(drawdown_pct),
                    days_to_trough=int(trough_idx - peak_idx),
                    recovery_days=int(i - trough_idx),
                    regime=classify_regime(drawdown_pct, trough_idx - peak_idx),
                ))
                in_drawdown = False
                trough_idx = None
                trough_price = None
            peak_idx = i
            peak_price = price
            continue

        # Prüfe auf Drawdown-Eintritt oder neuen Tiefpunkt
        loss = (price - peak_price) / peak_price
        if loss < -threshold:
            if not in_drawdown:
                in_drawdown = True
                trough_idx = i
                trough_price = price
            elif price < trough_price:
                trough_idx = i
                trough_price = price

    # Am Ende noch offene Drawdown-Phase?
    if in_drawdown and trough_idx is not None:
        drawdown_pct = (trough_price - peak_price) / peak_price
        events.append(DrawdownEvent(
            ticker="",
            peak_date=str(dates[peak_idx].date()),
            peak_price=float(peak_price),
            trough_date=str(dates[trough_idx].date()),
            trough_price=float(trough_price),
            drawdown_pct=float(drawdown_pct),
            days_to_trough=int(trough_idx - peak_idx),
            recovery_days=None,  # Noch nicht recovered
            regime=classify_regime(drawdown_pct, trough_idx - peak_idx),
        ))

    return events


def classify_regime(drawdown_pct: float, days_to_trough: int) -> str:
    """Einfache heuristische Klassifikation für menschliche Lesbarkeit."""
    mag = abs(drawdown_pct)
    if mag < 0.20:
        return "bull_correction"
    if mag < 0.35:
        return "bear_correction" if days_to_trough > 60 else "sharp_correction"
    if days_to_trough < 30:
        return "black_swan"
    return "bear_market"


# ────────────────────────────────────────────────────────────
# FEATURE EXTRACTION
# ────────────────────────────────────────────────────────────
def compute_features(prices: pd.DataFrame, at_idx: int) -> Optional[FeatureVector]:
    """
    Berechne Feature-Vektor für den Zustand am Index `at_idx`.
    Gibt None zurück, wenn nicht genug historische Daten verfügbar sind.
    """
    if at_idx < 200:
        return None

    close = prices["close"].values
    volume = prices["volume"].values
    current = close[at_idx]

    # Renditen über verschiedene Zeitfenster
    ret_30d  = current / close[at_idx - 30]  - 1
    ret_90d  = current / close[at_idx - 90]  - 1
    ret_180d = current / close[at_idx - 180] - 1

    # Annualisierte 30d-Volatilität
    returns_30d = np.diff(close[at_idx - 30:at_idx + 1]) / close[at_idx - 30:at_idx]
    vol_30d = float(np.std(returns_30d) * np.sqrt(252))

    # RSI (14)
    rsi = _compute_rsi(close[max(0, at_idx - 30):at_idx + 1], period=14)

    # Kurs relativ zu gleitenden Durchschnitten
    ma50  = float(np.mean(close[at_idx - 50:at_idx + 1]))
    ma200 = float(np.mean(close[at_idx - 200:at_idx + 1]))
    price_vs_ma50  = current / ma50  - 1
    price_vs_ma200 = current / ma200 - 1

    # Volumen-Trend: lineare Regressions-Steigung über 30 Tage, normiert
    vol_window = volume[at_idx - 30:at_idx + 1].astype(float)
    if np.mean(vol_window) > 0:
        x = np.arange(len(vol_window))
        slope = float(np.polyfit(x, vol_window, 1)[0])
        vol_trend = slope / np.mean(vol_window)
    else:
        vol_trend = 0.0

    # Max-Drawdown im Vorjahr
    year_window = close[at_idx - 252:at_idx + 1] if at_idx >= 252 else close[:at_idx + 1]
    running_max = np.maximum.accumulate(year_window)
    dd_prior = float(np.min(year_window / running_max - 1))

    return FeatureVector(
        ret_30d=float(ret_30d),
        ret_90d=float(ret_90d),
        ret_180d=float(ret_180d),
        volatility_30d=vol_30d,
        rsi_14=float(rsi),
        price_vs_ma50=float(price_vs_ma50),
        price_vs_ma200=float(price_vs_ma200),
        volume_trend_30d=vol_trend,
        drawdown_prior_y=dd_prior,
    )


def _compute_rsi(prices: np.ndarray, period: int = 14) -> float:
    """Klassischer Wilder-RSI ohne externe Lib."""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ────────────────────────────────────────────────────────────
# PERSISTENCE
# ────────────────────────────────────────────────────────────
def save_patterns(ticker: str, prices: pd.DataFrame, events: list[DrawdownEvent]) -> int:
    """Speichere Drawdown-Events + Feature-Vektoren in patterns.db."""
    saved = 0
    with connect(PATTERNS_DB) as conn:
        for event in events:
            event.ticker = ticker

            # Event-Record einfügen
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO drawdown_events
                    (ticker, peak_date, peak_price, trough_date, trough_price,
                     drawdown_pct, days_to_trough, recovery_days, regime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.ticker, event.peak_date, event.peak_price,
                    event.trough_date, event.trough_price, event.drawdown_pct,
                    event.days_to_trough, event.recovery_days, event.regime,
                ),
            )
            if cur.rowcount == 0:
                continue  # Event war schon in DB
            event_id = cur.lastrowid

            # Feature-Vektoren zu verschiedenen Lookbacks berechnen
            try:
                peak_idx = prices.index.get_loc(pd.Timestamp(event.peak_date))
            except KeyError:
                continue

            for lb in LOOKBACKS_DAYS:
                feat_idx = peak_idx - lb
                if feat_idx < 200:
                    continue
                feat = compute_features(prices, feat_idx)
                if feat is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO pre_drawdown_features
                        (event_id, lookback_days, ret_30d, ret_90d, ret_180d,
                         volatility_30d, rsi_14, price_vs_ma50, price_vs_ma200,
                         volume_trend_30d, drawdown_prior_y)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, lb,
                        feat.ret_30d, feat.ret_90d, feat.ret_180d,
                        feat.volatility_30d, feat.rsi_14,
                        feat.price_vs_ma50, feat.price_vs_ma200,
                        feat.volume_trend_30d, feat.drawdown_prior_y,
                    ),
                )
            saved += 1
    return saved


# ────────────────────────────────────────────────────────────
# SIMILARITY SEARCH
# ────────────────────────────────────────────────────────────
def find_similar_patterns(
    current_features: FeatureVector,
    lookback_days: int = 7,
    top_k: int = 5,
) -> list[dict]:
    """
    Finde die K ähnlichsten historischen Pre-Drawdown-Muster.

    Methode: normalisierte euklidische Distanz im 9-dimensionalen Feature-Raum.
    Vor Vergleich werden alle Features z-standardisiert, damit keine Dimension
    dominiert.

    Returns:
        Liste der Top-K Matches: [{event, distance, outcome}, …]
    """
    with connect(PATTERNS_DB) as conn:
        cur = conn.execute(
            """
            SELECT e.ticker, e.peak_date, e.drawdown_pct, e.days_to_trough,
                   e.regime, e.recovery_days,
                   f.ret_30d, f.ret_90d, f.ret_180d, f.volatility_30d, f.rsi_14,
                   f.price_vs_ma50, f.price_vs_ma200, f.volume_trend_30d,
                   f.drawdown_prior_y
            FROM drawdown_events e
            JOIN pre_drawdown_features f ON f.event_id = e.id
            WHERE f.lookback_days = ?
            """,
            (lookback_days,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return []

    # Feature-Matrix aufbauen
    feature_cols = [
        "ret_30d", "ret_90d", "ret_180d", "volatility_30d", "rsi_14",
        "price_vs_ma50", "price_vs_ma200", "volume_trend_30d", "drawdown_prior_y",
    ]
    matrix = np.array([[r[c] for c in feature_cols] for r in rows])
    current = current_features.to_array()

    # Z-Standardisierung: aus historischer Population
    means = matrix.mean(axis=0)
    stds  = matrix.std(axis=0)
    stds[stds == 0] = 1.0

    matrix_norm  = (matrix - means) / stds
    current_norm = (current - means) / stds

    # Distanzen berechnen
    distances = np.linalg.norm(matrix_norm - current_norm, axis=1)
    order = np.argsort(distances)[:top_k]

    return [
        {
            "ticker":         rows[i]["ticker"],
            "peak_date":      rows[i]["peak_date"],
            "drawdown_pct":   rows[i]["drawdown_pct"],
            "days_to_trough": rows[i]["days_to_trough"],
            "regime":         rows[i]["regime"],
            "recovery_days":  rows[i]["recovery_days"],
            "distance":       float(distances[i]),
        }
        for i in order
    ]


# ────────────────────────────────────────────────────────────
# HIGH-LEVEL API
# ────────────────────────────────────────────────────────────
def mine_ticker(ticker: str, period: str = "10y") -> dict:
    """
    Komplette Pipeline für einen einzelnen Ticker: lade Daten, finde Drawdowns,
    speichere Muster. Gibt Zusammenfassungs-Statistik zurück.
    """
    print(f"\n⛏  Mining patterns for {ticker}…")
    prices = get_prices(ticker, period=period)

    if len(prices) < 300:
        return {"ticker": ticker, "error": "zu wenig Historie"}

    events = detect_drawdowns(prices)
    saved = save_patterns(ticker, prices, events)

    avg_dd = (
        np.mean([e.drawdown_pct for e in events]) if events else 0.0
    )

    print(f"   {len(events)} Drawdown-Events gefunden, {saved} neu gespeichert")
    if events:
        print(f"   Ø Drawdown: {avg_dd:+.1%}")
        worst = min(events, key=lambda e: e.drawdown_pct)
        print(f"   Worst:     {worst.drawdown_pct:+.1%} ab {worst.peak_date} ({worst.regime})")

    return {
        "ticker":        ticker,
        "events_found":  len(events),
        "events_saved":  saved,
        "avg_drawdown":  avg_dd,
        "worst":         min(events, key=lambda e: e.drawdown_pct) if events else None,
    }


def summary() -> None:
    """Druckt eine Übersicht über alle gespeicherten Patterns."""
    with connect(PATTERNS_DB) as conn:
        counts = conn.execute(
            "SELECT ticker, COUNT(*) as n, AVG(drawdown_pct) as avg_dd "
            "FROM drawdown_events GROUP BY ticker ORDER BY n DESC"
        ).fetchall()
        total_features = conn.execute(
            "SELECT COUNT(*) FROM pre_drawdown_features"
        ).fetchone()[0]

    print("\n" + "=" * 60)
    print("PATTERN LIBRARY SUMMARY")
    print("=" * 60)
    print(f"  Gesamt: {sum(r['n'] for r in counts)} Events · "
          f"{total_features} Feature-Vektoren")
    print(f"  {'Ticker':<10} {'Events':>8}  {'Ø Drawdown':>12}")
    print("  " + "-" * 40)
    for row in counts:
        print(f"  {row['ticker']:<10} {row['n']:>8}  {row['avg_dd']:>+11.1%}")
    print("=" * 60)

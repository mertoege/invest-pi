"""
Outcome-Tracker — schließt den Self-Learning-Loop.

Für jede daily_score-prediction der Vergangenheit:
  1. Prüft ob T+1d / T+7d / T+30d erreichbar sind
  2. Holt die realisierten Kursbewegungen
  3. Vergleicht Predicted-Alert-Level mit Real-Drawdown
  4. Schreibt outcome_json + outcome_correct zurück

Hintergrund (LESSONS_FOR_INVEST_PI.md):
  - Spezifika 1: 3 Outcome-Fenster (1d / 7d / 30d) statt eines.
  - Bug-Story 4: Snapshot-Quelle muss alle subject_ids aus predictions berücksichtigen,
    nicht nur Portfolio.
  - TL;DR 7 (Drift-Detection): hit-rate(7d) vs prior(7d), Warnung bei Delta >15pp.

Korrektheits-Definition (T+7d, primary):
  - alert_level >= 2 (Caution/Red): korrekt wenn max_drawdown_7d <= -5%
  - alert_level == 0 (Green):       korrekt wenn max_drawdown_7d > -5%
  - alert_level == 1 (Watch):       NICHT bewertet — outcome_correct = NULL
                                     (zu unscharf für binary-correctness)

Diese Definition ist absichtlich grob. Verfeinerung kommt aus Meta-Review.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .data_loader import get_prices
from .json_utils import safe_parse
from .predictions import (
    pending_outcomes,
    record_outcome,
    hit_rate,
    PredictionRecord,
)


# ────────────────────────────────────────────────────────────
# KONFIG
# ────────────────────────────────────────────────────────────
WINDOWS_DAYS = (1, 7, 30)
DRAWDOWN_THRESHOLD = -0.05    # -5% in 7d-Fenster = "Risiko realisierte sich"
DRIFT_WINDOW_DAYS = 7
DRIFT_DELTA_THRESHOLD = 0.15  # 15pp


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class WindowMeasurement:
    days:           int
    return_pct:     Optional[float]
    max_drawdown:   Optional[float]
    end_date:       Optional[str]
    n_observations: int = 0


# ────────────────────────────────────────────────────────────
# CORE MEASUREMENT
# ────────────────────────────────────────────────────────────
def _measure_window(prices: pd.DataFrame, start_date: dt.datetime, days: int) -> WindowMeasurement:
    """
    Misst Return + Max-Drawdown über ein Fenster ab start_date.
    Falls noch nicht genug Tage vergangen sind: return_pct=None.
    """
    end_date = start_date + dt.timedelta(days=days)
    now = dt.datetime.utcnow()
    if end_date > now:
        return WindowMeasurement(days=days, return_pct=None, max_drawdown=None,
                                 end_date=None, n_observations=0)

    # Filter prices auf das Fenster
    mask = (prices.index >= pd.Timestamp(start_date.date())) & \
           (prices.index <= pd.Timestamp(end_date.date()))
    window = prices.loc[mask]
    if len(window) < 2:
        return WindowMeasurement(days=days, return_pct=None, max_drawdown=None,
                                 end_date=str(end_date.date()),
                                 n_observations=len(window))

    start_price = float(window["close"].iloc[0])
    end_price   = float(window["close"].iloc[-1])
    if start_price <= 0:
        return WindowMeasurement(days=days, return_pct=None, max_drawdown=None,
                                 end_date=str(end_date.date()),
                                 n_observations=len(window))

    return_pct = (end_price / start_price) - 1.0

    # Max-Drawdown: tiefster Punkt unter laufendem Hoch
    running_max = window["close"].cummax()
    drawdowns = (window["close"] / running_max) - 1.0
    max_dd = float(drawdowns.min())

    return WindowMeasurement(
        days=days,
        return_pct=float(return_pct),
        max_drawdown=max_dd,
        end_date=str(window.index[-1].date()),
        n_observations=len(window),
    )


def _correctness_for_alert(alert_level: int, max_dd_7d: Optional[float]) -> Optional[int]:
    """
    Maps (alert_level, realized 7d drawdown) → 1 (correct) / 0 (wrong) / None (unmessbar).
    """
    if max_dd_7d is None:
        return None
    if alert_level == 1:
        # Watch ist absichtlich nicht binary korrekt-/falsch-bar
        return None
    risk_realized = max_dd_7d <= DRAWDOWN_THRESHOLD
    if alert_level >= 2:
        return 1 if risk_realized else 0
    if alert_level == 0:
        return 0 if risk_realized else 1
    return None


# ────────────────────────────────────────────────────────────
# PER-PREDICTION
# ────────────────────────────────────────────────────────────
def measure_outcome_for(pred: PredictionRecord) -> Optional[dict]:
    """
    Misst alle erreichbaren Fenster (1d/7d/30d) für eine Prediction.
    Returns:
      - dict mit measurements + correctness, oder
      - None falls überhaupt keine Messung möglich (z.B. Prediction zu jung).
    """
    if pred.subject_type != "ticker" or not pred.subject_id:
        return None

    output = safe_parse(pred.output_json or "{}", default={})
    alert_level = int(output.get("alert_level", 0))

    created = dt.datetime.fromisoformat(pred.created_at.replace(" ", "T"))

    try:
        prices = get_prices(pred.subject_id, period="3mo")
    except Exception as e:
        return {"error": f"price-fetch failed: {e}"}

    measurements = {}
    any_measured = False
    for d in WINDOWS_DAYS:
        m = _measure_window(prices, created, d)
        measurements[f"{d}d"] = {
            "return_pct":     m.return_pct,
            "max_drawdown":   m.max_drawdown,
            "end_date":       m.end_date,
            "n_observations": m.n_observations,
        }
        if m.return_pct is not None:
            any_measured = True

    if not any_measured:
        return None  # zu jung, später nochmal versuchen

    correctness = _correctness_for_alert(
        alert_level, measurements["7d"]["max_drawdown"]
    )
    return {
        "alert_level":     alert_level,
        "windows":         measurements,
        "correctness_basis": "max_drawdown_7d <= -5%" if alert_level != 1 else "watch_not_evaluated",
        "_correct":        correctness,
    }


# ────────────────────────────────────────────────────────────
# RUNNER
# ────────────────────────────────────────────────────────────
def run_tracker(
    job_source: str = "daily_score",
    older_than_days: int = 1,
    limit: int = 200,
) -> dict:
    """
    Polled pending predictions und schreibt outcomes zurück.

    Args:
        job_source:       welche Quelle messen ('daily_score' | 'monthly_dca' | ...).
        older_than_days:  Mindest-Alter in Tagen, default 1 (für T+1d-Messung).
        limit:            max Predictions pro Lauf (Sicherheitsgrenze).
    """
    pending = pending_outcomes(job_source=job_source,
                                older_than_days=older_than_days,
                                limit=limit)
    stats = {
        "checked":        len(pending),
        "measured":       0,
        "still_pending":  0,
        "errors":         0,
        "by_correctness": {"correct": 0, "wrong": 0, "neutral": 0},
    }

    for pred in pending:
        result = measure_outcome_for(pred)
        if result is None:
            stats["still_pending"] += 1
            continue
        if "error" in result:
            stats["errors"] += 1
            continue

        record_outcome(
            prediction_id=pred.id,
            outcome=result,
            correct=result.get("_correct"),
        )
        stats["measured"] += 1
        c = result.get("_correct")
        if c == 1:
            stats["by_correctness"]["correct"] += 1
        elif c == 0:
            stats["by_correctness"]["wrong"] += 1
        else:
            stats["by_correctness"]["neutral"] += 1

    return stats


# ────────────────────────────────────────────────────────────
# DRIFT DETECTION
# ────────────────────────────────────────────────────────────
def detect_drift(job_source: str = "daily_score",
                 window_days: int = DRIFT_WINDOW_DAYS) -> Optional[dict]:
    """
    Vergleicht hit-rate der letzten N Tage mit der davor.
    Liefert Warning-Dict wenn Delta größer als Threshold, sonst None.
    """
    recent = hit_rate(job_source, days=window_days)
    # Prior window: simulieren über zwei mal window mit Subtraktion
    full = hit_rate(job_source, days=window_days * 2)

    if recent["measured"] == 0 or full["measured"] == 0:
        return None
    prior_measured = full["measured"] - recent["measured"]
    prior_correct  = full["correct"]  - recent["correct"]
    if prior_measured <= 0:
        return None

    recent_rate = recent["correct"] / recent["measured"]
    prior_rate  = prior_correct / prior_measured
    delta = recent_rate - prior_rate

    if abs(delta) >= DRIFT_DELTA_THRESHOLD:
        direction = "DROP" if delta < 0 else "JUMP"
        return {
            "job_source":   job_source,
            "window_days":  window_days,
            "recent_rate":  recent_rate,
            "prior_rate":   prior_rate,
            "delta_pp":     delta,
            "direction":    direction,
            "message": (
                f"Drift {direction}: hit-rate {recent_rate:.0%} "
                f"vs prior {prior_rate:.0%} "
                f"(Δ {delta:+.0%} pp, Schwelle {DRIFT_DELTA_THRESHOLD:.0%})"
            ),
        }
    return None


if __name__ == "__main__":
    print("Invest-Pi · Outcome-Tracker")
    stats = run_tracker()
    print(f"  checked:       {stats['checked']}")
    print(f"  measured:      {stats['measured']}")
    print(f"  still pending: {stats['still_pending']}")
    print(f"  errors:        {stats['errors']}")
    print(f"  → {stats['by_correctness']}")

    drift = detect_drift()
    if drift:
        print(f"\n⚠ {drift['message']}")
    else:
        print("\n  Drift-Check: ok")

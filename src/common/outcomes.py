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

# data_loader wird lazy in measure_outcome_for importiert (yfinance optional)
from .json_utils import safe_parse
from .predictions import (
    pending_outcomes,
    record_outcome,
    hit_rate,
    PredictionRecord,
)
from .storage import LEARNING_DB, connect
from ..learning.reflection import generate_reflection


# ────────────────────────────────────────────────────────────
# KONFIG
# ────────────────────────────────────────────────────────────
WINDOWS_DAYS = (1, 7, 30)
DRAWDOWN_THRESHOLD_BASE = -0.05  # baseline for ~25% annual vol stocks
DRIFT_WINDOW_DAYS = 7
DRIFT_DELTA_THRESHOLD = 0.15  # 15pp
_VOL_BASELINE = 0.25  # 25% annual vol = "normal" stock


def _volatility_adjusted_threshold(
    output_json: str | None,
    prices: pd.DataFrame | None = None,
) -> float:
    """
    Per-Ticker Drawdown-Threshold basierend auf historischer Volatility.
    High-vol Ticker (PLTR ~50%) bekommen einen grosszuegigeren Threshold,
    Low-vol Ticker (KO ~15%) einen strengeren.
    """
    import numpy as np
    vol_annual = None

    # 1. Versuche vol aus var_risk Dimension im output_json
    if output_json:
        out = safe_parse(output_json, default={})
        for d in out.get("dimensions", []):
            if d.get("name") == "var_risk":
                vol_annual = d.get("evidence", {}).get("vol_annual")
                if vol_annual:
                    vol_annual = float(vol_annual)
                    break

    # 2. Fallback: berechne aus Preisdaten
    if vol_annual is None and prices is not None and len(prices) >= 60:
        try:
            returns = prices["close"].pct_change().dropna().values[-60:]
            vol_annual = float(np.std(returns) * np.sqrt(252))
        except Exception:
            pass

    if vol_annual is None or vol_annual <= 0:
        return DRAWDOWN_THRESHOLD_BASE

    # Skaliere proportional: doppelte Vol = doppelt so grosszuegiger Threshold
    scaled = DRAWDOWN_THRESHOLD_BASE * (vol_annual / _VOL_BASELINE)
    return max(-0.20, min(-0.02, scaled))


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
    # now muss naive sein damit Vergleich mit naive end_date (aus iso-string) geht
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
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


def _correctness_for_alert(
    alert_level: int,
    max_dd_7d: Optional[float],
    threshold: float = DRAWDOWN_THRESHOLD_BASE,
) -> Optional[int]:
    """
    Maps (alert_level, realized 7d drawdown) → 1 (correct) / 0 (wrong) / None (unmessbar).
    Threshold is per-ticker volatility-adjusted.
    """
    if max_dd_7d is None:
        return None
    if alert_level == 1:
        return None
    risk_realized = max_dd_7d <= threshold
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
        from .data_loader import get_prices
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

    has_7d = measurements["7d"]["max_drawdown"] is not None
    if not has_7d and alert_level != 1:
        return None  # 7d noch nicht abgelaufen, später nochmal versuchen

    # Per-ticker volatility-adjusted threshold
    threshold = _volatility_adjusted_threshold(pred.output_json, prices)

    correctness = _correctness_for_alert(
        alert_level, measurements["7d"]["max_drawdown"], threshold
    )

    # Multi-Horizon: auch 1d und 30d separat bewerten
    correctness_1d = _correctness_for_alert(
        alert_level, measurements["1d"]["max_drawdown"], threshold
    ) if measurements["1d"]["max_drawdown"] is not None else None
    correctness_30d = _correctness_for_alert(
        alert_level, measurements["30d"]["max_drawdown"], threshold
    ) if measurements["30d"]["max_drawdown"] is not None else None

    return {
        "alert_level":     alert_level,
        "windows":         measurements,
        "correctness_basis": f"max_drawdown <= {threshold:.1%} (vol-adjusted)",
        "threshold_used":  round(threshold, 4),
        "_correct":        correctness,
        "_correct_1d":     correctness_1d,
        "_correct_30d":    correctness_30d,
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
            record_outcome(
                prediction_id=pred.id,
                outcome=result,
                correct=None,
            )
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

        # ── Auto-Feedback (ersetzt manuelle Telegram-Buttons) ──
        if c is not None:
            _auto_feedback(pred.id, result)

        # ── Reflection generieren (Self-Learning-Loop) ──────
        try:
            generate_reflection(
                prediction_id=pred.id,
                ticker=pred.subject_id,
                alert_level=int(result.get("alert_level", 0)),
                outcome_correct=result.get("_correct"),
                outcome_data=result,
                output_json=pred.output_json,
            )
        except Exception as _refl_err:
            import logging
            logging.getLogger("invest_pi.outcomes").warning(
                f"reflection generation failed for pred {pred.id}: {_refl_err}"
            )

    return stats


# ────────────────────────────────────────────────────────────
# DRIFT DETECTION
# ────────────────────────────────────────────────────────────
def _auto_feedback(prediction_id: int, result: dict) -> None:
    """
    Automatisches Feedback basierend auf gemessenem Outcome.
    Ersetzt die manuellen Telegram-Inline-Buttons.

    correct=1 + alert>=2 → alert war berechtigt (reason: "auto:confirmed")
    correct=0 + alert>=2 → false positive (reason: "auto:fp")
    correct=1 + alert==0 → green war korrekt (reason: "auto:confirmed")
    correct=0 + alert==0 → green war falsch, haette warnen sollen (reason: "auto:missed")
    """
    c = result.get("_correct")
    alert = result.get("alert_level", 0)
    if c is None:
        return

    if alert >= 2:
        fb_type = "confirmed" if c == 1 else "false_positive"
        reason_code = "auto:confirmed" if c == 1 else "auto:fp"
    elif alert == 0:
        fb_type = "confirmed" if c == 1 else "missed_risk"
        reason_code = "auto:confirmed" if c == 1 else "auto:missed"
    else:
        return

    dd_7d = result.get("windows", {}).get("7d", {}).get("max_drawdown")
    reason_text = f"7d-drawdown={dd_7d:.1%}" if dd_7d is not None else "auto"

    try:
        with connect(LEARNING_DB) as conn:
            existing = conn.execute(
                "SELECT id FROM feedback_reasons WHERE prediction_id = ? AND reason_code LIKE 'auto:%'",
                (prediction_id,),
            ).fetchone()
            if existing:
                return
            conn.execute(
                """INSERT INTO feedback_reasons
                   (prediction_id, feedback_type, reason_code, reason_text)
                   VALUES (?, ?, ?, ?)""",
                (prediction_id, fb_type, reason_code, reason_text),
            )
    except Exception:
        pass


def _dca_return_between(ticker: str, start_date, end_date) -> Optional[float]:
    """Return des Tickers vom ersten Handelstag >= start bis letztem <= end.
    None wenn keine Daten."""
    from .data_loader import get_prices
    try:
        px = get_prices(ticker, period="1y")
    except Exception:
        return None
    if px is None or len(px) == 0 or "close" not in px:
        return None
    try:
        dates = pd.to_datetime(px.index).date
        close = px["close"]
        sel_start = close[dates >= start_date]
        sel_end = close[dates <= end_date]
        if len(sel_start) == 0 or len(sel_end) == 0:
            return None
        p0 = float(sel_start.iloc[0])
        p1 = float(sel_end.iloc[-1])
        if p0 <= 0:
            return None
        return p1 / p0 - 1
    except Exception:
        return None


def measure_dca_outcomes(horizon_days: int = 30, limit: int = 200) -> dict:
    """Misst monthly_dca-Outcomes per FORWARD-RETURN ueber horizon_days.
    Skill-Metrik: hat der gewaehlte Titel den breiten Markt (SPY) geschlagen?
    outcome_correct=1 wenn Pick-Return >= SPY-Return. verdict=skip -> nicht
    messbar (markiert). Schliesst den DCA-Lern-Loop — calibration_block(
    'monthly_dca') liest die so entstehende hit_rate und speist sie in den
    naechsten DCA-Prompt. Wichtig fuer Echtgeld-DCA."""
    from .storage import LEARNING_DB, connect
    stats = {"checked": 0, "measured": 0, "correct": 0, "skipped": 0,
             "pending": 0, "errors": 0}
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(
            """SELECT id, created_at, output_json FROM predictions
                WHERE job_source='monthly_dca'
                  AND outcome_correct IS NULL AND outcome_json IS NULL
                  AND created_at <= datetime('now', ?)
                ORDER BY created_at LIMIT ?""",
            (f"-{horizon_days} days", limit),
        ).fetchall()
        for r in rows:
            stats["checked"] += 1
            out = safe_parse(r["output_json"] or "{}", default={})
            verdict = out.get("verdict", "skip")
            if verdict not in ("buy_single", "buy_etf"):
                conn.execute(
                    "UPDATE predictions SET outcome_json=?, outcome_measured_at=datetime('now') WHERE id=?",
                    (json.dumps({"dca_skip": True}), r["id"]))
                stats["skipped"] += 1
                continue
            etf = (out.get("alternative_etf") or "SPY").upper()
            ticker = ((out.get("ticker") if verdict == "buy_single" else etf) or etf).upper()
            try:
                created = dt.datetime.fromisoformat(
                    str(r["created_at"]).replace("Z", "").split(".")[0]).date()
            except Exception:
                stats["errors"] += 1
                continue
            end = created + dt.timedelta(days=horizon_days)
            pick_ret = _dca_return_between(ticker, created, end)
            if pick_ret is None:
                stats["pending"] += 1
                continue
            spy_ret = _dca_return_between("SPY", created, end)
            bench = spy_ret if spy_ret is not None else 0.0
            correct = 1 if pick_ret >= bench else 0
            oj = {"pick": ticker, "pick_return": round(pick_ret, 4),
                  "benchmark": "SPY",
                  "benchmark_return": round(spy_ret, 4) if spy_ret is not None else None,
                  "outperformance": round(pick_ret - bench, 4),
                  "horizon_days": horizon_days}
            conn.execute(
                "UPDATE predictions SET outcome_correct=?, outcome_json=?, "
                "outcome_measured_at=datetime('now') WHERE id=?",
                (correct, json.dumps(oj), r["id"]))
            stats["measured"] += 1
            stats["correct"] += correct
    return stats


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


def detect_dimension_drift(
    job_source: str = "daily_score",
    window_days: int = 14,
    min_samples: int = 10,
) -> list[dict]:
    """
    Per-Dimension Drift Detection.

    Vergleicht fuer jede Risk-Dimension die Hit-Rate der letzten N Tage
    mit der vorherigen Periode. Warnt wenn eine einzelne Dimension
    signifikant schlechter wird (>20pp Drop).

    Returns:
        Liste von Drift-Warnungen (leer = alles ok).
    """
    from .json_utils import safe_parse
    from .storage import LEARNING_DB, connect

    sql = """
        SELECT output_json, outcome_correct, created_at
          FROM predictions
         WHERE job_source = ?
           AND outcome_correct IN (0, 1)
           AND created_at >= datetime('now', ?)
         ORDER BY created_at
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (job_source, f"-{window_days * 2} day")).fetchall()

    if len(rows) < min_samples * 2:
        return []

    # Split in recent und prior
    midpoint = len(rows) // 2
    prior_rows = rows[:midpoint]
    recent_rows = rows[midpoint:]

    def dim_hit_rates(subset):
        rates = {}  # {dim_name: {"triggered_correct": n, "triggered_total": n}}
        for r in subset:
            out = safe_parse(r["output_json"] or "{}", default={})
            correct = r["outcome_correct"]
            for d in out.get("dimensions", []):
                name = d.get("name")
                triggered = d.get("triggered", False)
                if not name or not triggered:
                    continue
                if name not in rates:
                    rates[name] = {"correct": 0, "total": 0}
                rates[name]["total"] += 1
                if correct == 1:
                    rates[name]["correct"] += 1
        return rates

    prior_rates = dim_hit_rates(prior_rows)
    recent_rates = dim_hit_rates(recent_rows)

    warnings = []
    for dim_name in set(list(prior_rates.keys()) + list(recent_rates.keys())):
        p = prior_rates.get(dim_name, {"correct": 0, "total": 0})
        r = recent_rates.get(dim_name, {"correct": 0, "total": 0})

        if p["total"] < 3 or r["total"] < 3:
            continue

        p_rate = p["correct"] / p["total"]
        r_rate = r["correct"] / r["total"]
        delta = r_rate - p_rate

        if abs(delta) >= 0.20:  # 20pp threshold
            direction = "DROP" if delta < 0 else "JUMP"
            warnings.append({
                "dimension": dim_name,
                "direction": direction,
                "prior_rate": round(p_rate, 3),
                "recent_rate": round(r_rate, 3),
                "delta_pp": round(delta, 3),
                "prior_n": p["total"],
                "recent_n": r["total"],
                "message": (
                    f"Dim-Drift {direction}: {dim_name} "
                    f"{p_rate:.0%} → {r_rate:.0%} (Δ {delta:+.0%})"
                ),
            })

    warnings.sort(key=lambda w: abs(w["delta_pp"]), reverse=True)
    return warnings


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

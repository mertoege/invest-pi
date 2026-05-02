"""
Regime-Outcome Tracking — zeichnet auf, welches Regime bei jeder Prediction
aktiv war, und liefert per-regime Hit-Rate-Analysen.

Damit kann der Meta-Review erkennen:
  - "In bear-Regime war unsere Hit-Rate 80% — unsere Risk-Signale funktionieren"
  - "In low_vol_bull lag die Hit-Rate nur bei 40% — zu viele false positives im Aufschwung"
  → Regime-spezifische Schwellen-Anpassung

Usage:
    # Bei jeder Prediction:
    snap_regime(prediction_id=42)

    # Fuer Meta-Review/Calibration:
    stats = hit_rate_by_regime(job_source="daily_score", days=60)
"""

from __future__ import annotations

import logging
from typing import Optional, Any

from ..common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.regime_tracker")


def snap_regime(prediction_id: Optional[int] = None) -> Optional[dict]:
    """
    Nimmt einen Snapshot des aktuellen Regimes und speichert ihn.
    Wird nach jedem log_prediction() aufgerufen.

    Returns:
        dict mit regime_label, probability, method oder None bei Fehler.
    """
    try:
        from .regime import current_regime
        r = current_regime()
    except Exception as e:
        log.debug(f"regime detection failed: {e}")
        return None

    vix_level = None
    try:
        from ..common.data_loader import get_prices
        vix_data = get_prices("^VIX", period="5d")
        if not vix_data.empty:
            vix_level = float(vix_data["close"].iloc[-1])
    except Exception:
        pass

    try:
        with connect(LEARNING_DB) as conn:
            conn.execute(
                """
                INSERT INTO regime_snapshots
                    (regime_label, probability, method, vix_level, prediction_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (r.label, r.probability, r.method, vix_level, prediction_id),
            )
    except Exception as e:
        log.warning(f"regime snapshot insert failed: {e}")
        return None

    return {
        "regime_label": r.label,
        "probability": r.probability,
        "method": r.method,
        "vix_level": vix_level,
    }


def hit_rate_by_regime(
    job_source: str = "daily_score",
    days: int = 60,
) -> list[dict]:
    """
    Hit-Rate aufgeschluesselt nach Regime.

    Returns:
        [
          {"regime": "low_vol_bull", "total": 50, "measured": 30,
           "correct": 18, "hit_rate": 0.60, "avg_vix": 15.2},
          {"regime": "bear", ...},
          ...
        ]
    """
    sql = """
        SELECT
            rs.regime_label,
            COUNT(*)                                          AS total,
            SUM(CASE WHEN p.outcome_correct IN (0,1) THEN 1 ELSE 0 END) AS measured,
            SUM(CASE WHEN p.outcome_correct = 1 THEN 1 ELSE 0 END)      AS correct,
            AVG(rs.vix_level)                                 AS avg_vix
          FROM regime_snapshots rs
          JOIN predictions p ON rs.prediction_id = p.id
         WHERE p.job_source = ?
           AND p.created_at >= datetime('now', ?)
         GROUP BY rs.regime_label
         ORDER BY total DESC
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (job_source, f"-{days} day")).fetchall()

    results = []
    for r in rows:
        measured = int(r["measured"])
        correct = int(r["correct"])
        results.append({
            "regime": r["regime_label"],
            "total": int(r["total"]),
            "measured": measured,
            "correct": correct,
            "hit_rate": round(correct / measured, 3) if measured > 0 else None,
            "avg_vix": round(float(r["avg_vix"]), 1) if r["avg_vix"] else None,
        })
    return results


def regime_calibration_block(
    job_source: str = "daily_score",
    days: int = 60,
) -> str:
    """
    Markdown-Block fuer den Calibration-Prompt.
    Zeigt per-regime Hit-Rate, damit Sonnet regime-aware scoren kann.
    """
    stats = hit_rate_by_regime(job_source, days)
    if not stats:
        return ""

    parts = [f"## Hit-Rate nach Regime ({days}d)"]
    for s in stats:
        hr = f"{s['hit_rate']:.0%}" if s["hit_rate"] is not None else "n/a"
        vix = f", avg VIX {s['avg_vix']}" if s["avg_vix"] else ""
        parts.append(
            f"  {s['regime']}: {s['correct']}/{s['measured']} korrekt "
            f"({hr}){vix}"
        )
    parts.append("")
    return "\n".join(parts)

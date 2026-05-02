"""
Weight-Optimizer — Dynamische Anpassung der DIMENSION_WEIGHTS.

Schliesst die letzte grosse Luecke im Self-Learning-Loop:
  Attribution berechnet welche Dimensionen korrekt vorhersagen (separation > 0)
  und welche nur Noise erzeugen (separation < 0). Dieses Modul nimmt diese
  Ergebnisse und passt die Gewichte im Risk-Scorer an.

Methode (inspiriert von TradingAgents + SAGE):
  1. Lade attribution_dimensions() fuer die letzten 30-60 Tage
  2. Normalisiere Separations zu relativen Gewichten
  3. Blend mit den Default-Gewichten (70% empirisch / 30% default)
  4. Clamp auf [0.3, 2.0] damit keine Dimension komplett ignoriert wird
  5. Speichere als JSON in learning.db fuer Audit-Trail

Wird woechentlich via meta_review oder eigenem Timer aufgerufen.
"""

from __future__ import annotations

import json
import datetime as dt
from typing import Optional

from .attribution import attribute_dimensions
from ..common.storage import LEARNING_DB, connect


# Default-Gewichte (Fallback wenn nicht genug Daten)
DEFAULT_WEIGHTS = {
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

# Sicherheits-Grenzen
MIN_WEIGHT = 0.3
MAX_WEIGHT = 2.0
BLEND_EMPIRICAL = 0.7   # 70% empirisch, 30% default
MIN_SAMPLES_PER_DIM = 15  # Mindest-Samples bevor empirisch genutzt wird


def compute_optimal_weights(
    job_source: str = "daily_score",
    days: int = 60,
) -> dict[str, float]:
    """
    Berechnet optimale Gewichte basierend auf empirischer Attribution.

    Returns:
        Dict {dim_name: weight} mit Werten zwischen MIN_WEIGHT und MAX_WEIGHT
    """
    attribs = attribute_dimensions(job_source, days=days)

    if not attribs:
        return DEFAULT_WEIGHTS.copy()

    # Nur Dimensionen mit genug Samples verwenden
    valid = [a for a in attribs if a["n_total"] >= MIN_SAMPLES_PER_DIM]
    if len(valid) < 3:
        return DEFAULT_WEIGHTS.copy()

    # Separation → relative Gewichte
    # Positive Separation = Dimension ist praediktiv → hoeheres Gewicht
    # Negative Separation = Dimension erzeugt Noise → niedrigeres Gewicht
    separations = {a["name"]: a["separation"] for a in valid}

    # Normalisiere: shift so dass min=0.5, max=2.0
    sep_values = list(separations.values())
    sep_min = min(sep_values)
    sep_max = max(sep_values)
    sep_range = sep_max - sep_min

    if sep_range < 1.0:
        # Alle Dimensionen performen aehnlich → Default behalten
        return DEFAULT_WEIGHTS.copy()

    empirical = {}
    for name, sep in separations.items():
        # Linear mapping: worst separation → 0.5, best → 2.0
        normalized = 0.5 + 1.5 * (sep - sep_min) / sep_range
        empirical[name] = normalized

    # Blend mit Defaults
    result = {}
    for name, default_w in DEFAULT_WEIGHTS.items():
        if name in empirical:
            blended = BLEND_EMPIRICAL * empirical[name] + (1 - BLEND_EMPIRICAL) * default_w
        else:
            blended = default_w
        result[name] = round(max(MIN_WEIGHT, min(MAX_WEIGHT, blended)), 3)

    return result


def apply_weights(weights: dict[str, float]) -> None:
    """
    Wendet neue Gewichte auf risk_scorer.DIMENSION_WEIGHTS an (Runtime-Patch).
    Aenderung gilt bis zum naechsten Python-Restart.
    """
    from ..alerts.risk_scorer import DIMENSION_WEIGHTS
    for name, w in weights.items():
        if name in DIMENSION_WEIGHTS:
            DIMENSION_WEIGHTS[name] = w


def save_weight_snapshot(
    weights: dict[str, float],
    source: str = "auto_optimizer",
    notes: str = "",
) -> int:
    """Speichert Gewichte-Snapshot in learning.db fuer Audit-Trail."""
    with connect(LEARNING_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO weight_snapshots (weights_json, source, notes)
            VALUES (?, ?, ?)
            """,
            (json.dumps(weights), source, notes),
        )
        return int(cur.lastrowid)


def load_latest_weights() -> Optional[dict[str, float]]:
    """Laedt die juengsten gespeicherten Gewichte (fuer Startup-Recovery)."""
    with connect(LEARNING_DB) as conn:
        row = conn.execute(
            "SELECT weights_json FROM weight_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["weights_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def optimize_and_apply(
    job_source: str = "daily_score",
    days: int = 60,
    dry_run: bool = False,
) -> dict:
    """
    Haupteinstiegspunkt: berechnet, speichert und wendet Gewichte an.

    Returns:
        Report-Dict mit alten/neuen Gewichten und Deltas
    """
    old_weights = DEFAULT_WEIGHTS.copy()
    # Versuche vorherige Gewichte zu laden
    prev = load_latest_weights()
    if prev:
        old_weights = prev

    new_weights = compute_optimal_weights(job_source, days)

    # Delta berechnen
    deltas = {}
    for name in DEFAULT_WEIGHTS:
        old_w = old_weights.get(name, 1.0)
        new_w = new_weights.get(name, 1.0)
        deltas[name] = {
            "old": round(old_w, 3),
            "new": round(new_w, 3),
            "delta": round(new_w - old_w, 3),
        }

    report = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "job_source": job_source,
        "days": days,
        "dry_run": dry_run,
        "weights": new_weights,
        "deltas": deltas,
        "changed": any(d["delta"] != 0 for d in deltas.values()),
    }

    if not dry_run:
        apply_weights(new_weights)
        save_weight_snapshot(new_weights, notes=f"auto-optimized from {days}d attribution")

    return report

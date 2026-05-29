"""
Performance-Attribution-Layer.

Beantwortet die Frage: WELCHE Risk-Dimensionen liefern eigentlich Signal?

WICHTIG (2026-05-30): Attribution misst jetzt gegen REALISIERTE Forward-Returns
(return_7d aus reflections), NICHT mehr gegen das outcome_correct-Flag. Grund:
outcome_correct ist basisraten-dominiert (95% 'Green = kein Crash'), eine
Dimension konnte hohe 'separation' zeigen indem sie nur die Basisrate nachbildet
— ohne echte Vorhersagekraft fuer Rendite/Drawdown. Jetzt zaehlt, ob ein hoher
Dimensions-Score eine SCHWAECHERE Forward-Rendite vorhersagt (= echtes Risiko).

Methode pro Dimension: teile measured predictions (mit realisiertem return_7d)
am Median des Dimensions-Scores in 'hoher Score' vs 'niedriger Score'. Separation
= mean_return(niedrig) - mean_return(hoch), in Prozentpunkten.
  separation > 0  → hoher Score sagt schwaechere Rendite voraus = PRAEDIKTIV
  separation < 0  → hoher Score sagt staerkere Rendite voraus = KONTRAER/anti
Dimensionen ohne ausreichende Stichprobe in beiden Gruppen werden ausgelassen
(behalten im Optimizer ihr Default-Gewicht statt faelschlich abgewertet zu werden).

Wird in daily_report (Sonntag), meta_review und weight_optimizer genutzt.
"""

from __future__ import annotations

import statistics
from typing import Optional

from ..common.json_utils import safe_parse
from ..common.storage import LEARNING_DB, connect

# Mindest-Stichprobe pro Score-Gruppe (hoch/niedrig), sonst Dimension auslassen.
_MIN_PER_GROUP = 20


def attribute_dimensions(job_source: str = "daily_score", days: int = 30) -> list[dict]:
    """
    Returns Liste von Dim-Stats, sortiert nach separation descending.

    Format pro Dim:
        {
            "name":               "technical_breakdown",
            "n_total":            420,
            "avg_return_high":    -0.031,   # mittlere 7d-Rendite bei hohem Score
            "avg_return_low":     -0.009,   # mittlere 7d-Rendite bei niedrigem Score
            "separation":          2.2,     # (low - high) * 100, >0 = praediktiv
            "n_triggered":         55,
            "hit_rate_triggered":  0.61,    # Anteil getriggerter mit return_7d < 0
        }
    """
    sql = """
        SELECT p.output_json AS output_json, r.return_7d AS return_7d
          FROM predictions p
          JOIN reflections r ON r.prediction_id = p.id
         WHERE p.job_source = ?
           AND r.return_7d IS NOT NULL
           AND p.created_at >= datetime('now', ?)
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (job_source, f"-{days} day")).fetchall()

    if not rows:
        return []

    # Per-dim: Liste (score, fwd_return) + getriggerte Returns
    acc: dict[str, dict] = {}
    for r in rows:
        out = safe_parse(r["output_json"] or "{}", default={})
        fwd = float(r["return_7d"])
        for d in out.get("dimensions", []):
            name = d.get("name")
            if not name:
                continue
            if name not in acc:
                acc[name] = {"pairs": [], "trig_returns": []}
            score = float(d.get("score", 0))
            acc[name]["pairs"].append((score, fwd))
            if bool(d.get("triggered", False)):
                acc[name]["trig_returns"].append(fwd)

    result = []
    for name, data in acc.items():
        pairs = data["pairs"]
        n_total = len(pairs)
        if n_total < 2 * _MIN_PER_GROUP:
            continue  # zu wenig Daten -> auslassen (Optimizer behaelt Default)
        scores = sorted(p[0] for p in pairs)
        median = scores[len(scores) // 2]
        high = [fwd for s, fwd in pairs if s > median]
        low = [fwd for s, fwd in pairs if s <= median]
        if len(high) < _MIN_PER_GROUP or len(low) < _MIN_PER_GROUP:
            continue  # Score zu degeneriert (z.B. fast nur Nullen) -> auslassen
        avg_high = statistics.mean(high)
        avg_low = statistics.mean(low)
        separation = (avg_low - avg_high) * 100  # >0 = hoher Score -> schwaechere Rendite
        trig = data["trig_returns"]
        n_trig = len(trig)
        hit_rate_trig = (sum(1 for x in trig if x < 0) / n_trig) if n_trig > 0 else None
        result.append({
            "name":               name,
            "n_total":            n_total,
            "avg_return_high":    round(avg_high, 4),
            "avg_return_low":     round(avg_low, 4),
            "separation":         round(separation, 2),
            "n_triggered":        n_trig,
            "hit_rate_triggered": round(hit_rate_trig, 3) if hit_rate_trig is not None else None,
        })
    result.sort(key=lambda x: -x["separation"])
    return result


def attribution_block(job_source: str = "daily_score", days: int = 30) -> str:
    """Telegram-HTML-Block fuer daily_report + Prompt-Injection."""
    rows = attribute_dimensions(job_source, days)
    if not rows:
        return ""
    parts = [f"📐 <b>Risk-Dim-Attribution ({days}d, {job_source})</b>"]
    for r in rows[:9]:
        sep_emoji = "🟢" if r["separation"] > 0.5 else ("🔴" if r["separation"] < -0.5 else "⚪")
        hit = (f"{r['hit_rate_triggered']*100:.0f}%" if r["hit_rate_triggered"] is not None else "—")
        parts.append(
            f"  {sep_emoji} <code>{r['name']:<22}</code> "
            f"sep:{r['separation']:+5.1f}pp | trig {r['n_triggered']:>3} ({hit})"
        )
    parts.append("\n<i>sep > 0 = hoher Score sagt schwächere 7d-Rendite voraus (prädiktiv)</i>")
    return "\n".join(parts)

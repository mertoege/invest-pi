"""
Performance-Attribution-Layer.

Beantwortet die Frage: WELCHE Risk-Dimensionen liefern eigentlich das
Profit-Signal? Aktuell haben wir 9 Dimensionen mit hartcodierten Gewichten —
ohne Attribution wissen wir nie welche davon Geld macht und welche nur Noise
addet.

Methode: fuer jede measured prediction (outcome_correct in {0,1}) extrahieren
wir die 9 dimension-scores aus output_json. Pro Dimension berechnen wir:
  - avg_score_correct:   mittlerer dim.score wenn outcome=1
  - avg_score_incorrect: mittlerer dim.score wenn outcome=0
  - separation:          Differenz (high score = "predictiv")
  - n_triggered_correct / n_triggered_incorrect: bei dim.triggered=True
  - hit_rate_when_triggered

Wird in daily_report (Sonntag), meta_review und calibration_block genutzt.
"""

from __future__ import annotations

import statistics
from typing import Optional

from ..common.json_utils import safe_parse
from ..common.storage import LEARNING_DB, connect


def attribute_dimensions(job_source: str = "daily_score", days: int = 30) -> list[dict]:
    """
    Returns Liste von Dim-Stats, sortiert nach |separation| descending.

    Format pro Dim:
        {
            "name":               "technical_breakdown",
            "n_total":            42,
            "n_correct":          25,
            "n_incorrect":        17,
            "avg_score_correct":   55.4,
            "avg_score_incorrect": 38.1,
            "separation":         17.3,    # > 0: hoher score koreliert mit correct outcome
            "n_triggered":        18,
            "hit_rate_triggered": 0.72,
        }
    """
    sql = """
        SELECT output_json, outcome_correct
          FROM predictions
         WHERE job_source = ?
           AND outcome_correct IN (0, 1)
           AND created_at >= datetime('now', ?)
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (job_source, f"-{days} day")).fetchall()

    if not rows:
        return []

    # Per-dim Akkumulator: {name: {correct_scores, incorrect_scores, triggered_correct, triggered_incorrect}}
    acc: dict[str, dict] = {}

    for r in rows:
        out = safe_parse(r["output_json"] or "{}", default={})
        outcome = r["outcome_correct"]
        for d in out.get("dimensions", []):
            name = d.get("name")
            if not name:
                continue
            if name not in acc:
                acc[name] = {
                    "correct_scores": [], "incorrect_scores": [],
                    "triggered_correct": 0, "triggered_incorrect": 0,
                }
            score = float(d.get("score", 0))
            triggered = bool(d.get("triggered", False))
            if outcome == 1:
                acc[name]["correct_scores"].append(score)
                if triggered: acc[name]["triggered_correct"] += 1
            else:
                acc[name]["incorrect_scores"].append(score)
                if triggered: acc[name]["triggered_incorrect"] += 1

    result = []
    for name, data in acc.items():
        cs = data["correct_scores"]
        ics = data["incorrect_scores"]
        n_correct = len(cs)
        n_incorrect = len(ics)
        n_total = n_correct + n_incorrect
        if n_total == 0:
            continue
        avg_c = statistics.mean(cs) if cs else 0.0
        avg_ic = statistics.mean(ics) if ics else 0.0
        separation = avg_c - avg_ic
        n_trig_c = data["triggered_correct"]
        n_trig_ic = data["triggered_incorrect"]
        n_trig = n_trig_c + n_trig_ic
        hit_rate_trig = (n_trig_c / n_trig) if n_trig > 0 else None
        result.append({
            "name":               name,
            "n_total":            n_total,
            "n_correct":          n_correct,
            "n_incorrect":        n_incorrect,
            "avg_score_correct":   round(avg_c, 1),
            "avg_score_incorrect": round(avg_ic, 1),
            "separation":         round(separation, 1),
            "n_triggered":        n_trig,
            "hit_rate_triggered": round(hit_rate_trig, 3) if hit_rate_trig is not None else None,
        })
    # Sort: positive separation oben (predictiv), neutral mittig, negativ unten
    result.sort(key=lambda x: -x["separation"])
    return result


def attribution_block(job_source: str = "daily_score", days: int = 30) -> str:
    """Telegram-HTML-Block fuer daily_report + Prompt-Injection."""
    rows = attribute_dimensions(job_source, days)
    if not rows:
        return ""
    parts = [f"📐 <b>Risk-Dim-Attribution ({days}d, {job_source})</b>"]
    for r in rows[:9]:
        sep_emoji = "🟢" if r["separation"] > 5 else ("🔴" if r["separation"] < -5 else "⚪")
        hit = (f"{r['hit_rate_triggered']*100:.0f}%" if r["hit_rate_triggered"] is not None else "—")
        parts.append(
            f"  {sep_emoji} <code>{r['name']:<22}</code> "
            f"sep:{r['separation']:+5.1f} | trig {r['n_triggered']:>3} ({hit})"
        )
    parts.append("\n<i>sep > 0 = hoher Score korreliert mit korrekter Vorhersage</i>")
    return "\n".join(parts)

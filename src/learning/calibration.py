"""
Calibration-Layer.

Liest die letzten meta_review-Outputs aus learning.db und formatiert sie als
Prompt-Injection-Block fuer alle nachfolgenden LLM-Calls (Score, DCA, etc.).

Damit wird der Self-Learning-Loop geschlossen: gemessene Outcomes →
Opus-Reflexion → Markdown-Aktionsplan → injected in nachfolgende Prompts →
naechste Decisions sind kalibriert ohne Code-Aenderung.

Plus: hit_rate_stratified-Snapshots werden frisch gerechnet und mitgeliefert
fuer "Mid-period self-correction" (LESSONS Pattern 1: Sonnet sieht in jedem
Score-Call seine eigene 30d-Hit-Rate).
"""

from __future__ import annotations

from typing import Optional

from ..common.json_utils import safe_parse
from ..common.predictions import feedback_summary, hit_rate_stratified
from ..common.storage import LEARNING_DB, connect


def latest_meta_review(job_source: str, max_age_days: int = 60) -> Optional[dict]:
    """Holt die juengste meta_review-Row fuer ein job_source."""
    sql = """
        SELECT id, created_at, period_start, period_end, summary_md, action_plan_js
          FROM meta_reviews
         WHERE job_source = ?
           AND created_at >= datetime('now', ?)
         ORDER BY created_at DESC LIMIT 1
    """
    with connect(LEARNING_DB) as conn:
        row = conn.execute(sql, (job_source, f"-{max_age_days} day")).fetchone()
    if not row:
        return None
    return {
        "id":           row["id"],
        "created_at":   row["created_at"],
        "period_start": row["period_start"],
        "period_end":   row["period_end"],
        "summary_md":   row["summary_md"] or "",
        "action_plan":  safe_parse(row["action_plan_js"] or "{}", default={}),
    }


def calibration_block(job_source: str = "daily_score",
                       hit_rate_days: int = 30,
                       feedback_days: int = 30) -> str:
    """
    Liefert einen Markdown-Block der in jeden Score- oder DCA-Prompt injected wird.

    Format:
        ## Aktuelle Lern-Statistik (job_source, 30d)
        ...hit-rate stratifiziert...

        ## User-Feedback-Patterns (feedback_reasons, 30d)
        ...

        ## Letzte Meta-Review-Erkenntnisse (Action-Plan)
        ...
    """
    parts = []

    # 1. Hit-Rate
    rates = hit_rate_stratified(job_source, days=hit_rate_days)
    o = rates["overall"]
    if o["measured"] > 0:
        parts.append(f"## Lern-Statistik ({job_source}, {hit_rate_days}d)")
        parts.append(
            f"Total: {o['total']} Predictions, {o['measured']} measured, "
            f"hit-rate {(o['hit_rate'] or 0):.0%}"
        )
        for level in ("high", "medium", "low"):
            s = rates[level]
            if s["measured"] > 0:
                parts.append(
                    f"  Konfidenz {level}: {s['correct']}/{s['measured']} korrekt "
                    f"({(s['hit_rate'] or 0):.0%})"
                )
        parts.append("")

    # 2. User-Feedback
    fb = feedback_summary(days=feedback_days)
    if fb["by_type_reason"]:
        parts.append(f"## User-Feedback (letzte {feedback_days}d)")
        for r in fb["by_type_reason"][:8]:
            label = r["feedback_type"]
            if r["reason_code"]:
                label += f" ({r['reason_code']})"
            parts.append(f"  {label}: {r['n']}x")
        parts.append("")

    # 3. Meta-Review
    review = latest_meta_review(job_source)
    if review and review["action_plan"]:
        parts.append("## Letzte Meta-Review-Erkenntnisse")
        plan = review["action_plan"]
        for prio in ("prio_1", "prio_2", "prio_3"):
            items = plan.get(prio, [])
            if items:
                parts.append(f"### {prio.replace('_', ' ').title()}")
                for item in items[:3]:
                    parts.append(f"  - {item}")
        parts.append("")

    if not parts:
        return ""
    return "\n".join(parts)

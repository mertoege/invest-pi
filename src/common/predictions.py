"""
Predictions-Layer — der Self-Learning-Loop-Anker.

Jede Score-Berechnung, jede Sonnet-Empfehlung, jeder Meta-Review schreibt
eine prediction-Row mit subject_id (= Ticker für ticker-bezogene Jobs).

Hintergrund (LESSONS_FOR_INVEST_PI.md):
  TL;DR Punkt 1: Self-Learning-Loop muss vom ersten Tag an mitlaufen.
  TL;DR Punkt 2: predictions in EINER Tabelle, mit prompt_hash + subject_id durchgängig.
  Bug-Story 1: subject_id muss IMMER gesetzt sein, sonst ist outcome-tracking unmöglich.
  Anti-Pattern 5: prompt_hash IMMER setzen (sha256[:16]), für späteres A/B.

Domain-agnostisch — kein Wissen über Ticker/Sektoren hier drin, das passiert
bei der job_source-spezifischen Logik.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

from .storage import LEARNING_DB, connect


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class PredictionRecord:
    """In-Memory-Repräsentation einer geloggten Prediction."""
    id:                 int
    created_at:         str
    job_source:         str
    model:              str
    prompt_hash:        Optional[str]
    input_hash:         Optional[str]
    input_summary:      Optional[str]
    output_json:        Optional[str]
    confidence:         Optional[str]
    subject_type:       Optional[str]
    subject_id:         Optional[str]
    input_tokens:       Optional[int]
    output_tokens:      Optional[int]
    cost_estimate_eur:  float
    outcome_json:       Optional[str]
    outcome_measured_at: Optional[str]
    outcome_correct:    Optional[int]


# ────────────────────────────────────────────────────────────
# HASH-HELPER
# ────────────────────────────────────────────────────────────
def hash_short(s: str) -> str:
    """Stabiler 16-char sha256 prefix. Wird für prompt_hash + input_hash genutzt."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


# ────────────────────────────────────────────────────────────
# WRITE
# ────────────────────────────────────────────────────────────
def log_prediction(
    *,
    job_source:        str,
    model:             str,
    subject_type:      str = "ticker",
    subject_id:        Optional[str] = None,
    prompt:            Optional[str] = None,
    input_payload:     Any = None,
    input_summary:     Optional[str] = None,
    output:            Any = None,
    confidence:        Optional[str] = None,
    input_tokens:      Optional[int] = None,
    output_tokens:     Optional[int] = None,
    cost_estimate_eur: float = 0.0,
    prompt_hash:       Optional[str] = None,
    input_hash:        Optional[str] = None,
) -> int:
    """
    Schreibt eine prediction-Row und gibt deren id zurück.

    Args (Keyword-only weil viele):
        job_source:    'daily_score' | 'monthly_dca' | 'meta_review' | 'score_batch' | ...
        model:         'heuristic-v1' | 'claude-sonnet-4-6' | 'claude-opus-4-6' | ...
        subject_type:  'ticker' (default) | 'portfolio' | 'sector' | 'batch'
        subject_id:    z.B. 'NVDA'. WICHTIG: für outcome-tracking unverzichtbar.
                       Bei job_source='score_batch' (= ein Sonnet-Call für mehrere
                       Tickers) → subject_type='batch' und sofort mark_batch_aggregate
                       aufrufen (kein outcome messbar).
        prompt:        System-Prompt-Text. Wird in prompt_hash gehashed.
        input_payload: Beliebige Struktur. Wird zu JSON serialisiert für input_hash.
        input_summary: kurze human-readable Beschreibung ('NVDA, 9 dims, 14d basis').
        output:        dict | str — wird zu JSON-String. Strings werden vorher
                       Markdown-Codefence-stripped.
        confidence:    'high' | 'medium' | 'low' | None.
        cost_estimate_eur: für Cost-Caps (calendar-day-Aggregation).

    Returns:
        prediction_id (für späteren record_outcome-Aufruf).
    """
    if subject_id is None and subject_type == "ticker":
        # Soft-warn aber nicht crashen — manche Jobs (z.B. macro_regime) sind
        # ticker-unabhängig.
        subject_type = "global"

    if prompt and prompt_hash is None:
        prompt_hash = hash_short(prompt)

    if input_payload is not None and input_hash is None:
        try:
            input_str = json.dumps(input_payload, default=str, sort_keys=True)
        except (TypeError, ValueError):
            input_str = str(input_payload)
        input_hash = hash_short(input_str)

    output_json = _normalize_output(output)

    with connect(LEARNING_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO predictions
                (job_source, model, prompt_hash, input_hash, input_summary,
                 output_json, confidence, subject_type, subject_id,
                 input_tokens, output_tokens, cost_estimate_eur)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_source, model, prompt_hash, input_hash, input_summary,
             output_json, confidence, subject_type, subject_id,
             input_tokens, output_tokens, cost_estimate_eur),
        )
        return int(cur.lastrowid)


def _normalize_output(output: Any) -> Optional[str]:
    """Output → JSON-String, mit Markdown-Strip falls bereits ein String."""
    if output is None:
        return None
    if isinstance(output, str):
        from .json_utils import strip_codefence
        return strip_codefence(output)
    try:
        return json.dumps(output, default=str)
    except (TypeError, ValueError):
        return str(output)


def record_outcome(
    prediction_id:   int,
    outcome:         Any,
    correct:         Optional[int] = None,
    measured_at:     Optional[str] = None,
) -> None:
    """
    Trägt das Outcome einer Prediction nach.

    Args:
        prediction_id: id aus log_prediction.
        outcome:       dict mit den gemessenen Werten (z.B.
                       {actual_close_7d: 132.5, max_drawdown_7d: -0.08}).
                       Wird zu JSON serialisiert.
        correct:       1 (Prediction war richtig) | 0 (war falsch) | None (unmessbar).
                       NULL ist legitim (z.B. Stub-Dimension oder batch_aggregate).
        measured_at:   ISO-String. Default: jetzt.

    Hinweis (LESSONS Bug-Story 1):
        Bei Predictions, die nie messbar sein können (Batch-Calls ohne subject_id),
        SOFORT mark_batch_aggregate aufrufen, nicht für immer pending lassen.
    """
    import datetime as dt
    if measured_at is None:
        measured_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

    outcome_json = json.dumps(outcome, default=str) if outcome is not None else None

    with connect(LEARNING_DB) as conn:
        conn.execute(
            """
            UPDATE predictions
               SET outcome_json = ?,
                   outcome_measured_at = ?,
                   outcome_correct = ?
             WHERE id = ?
            """,
            (outcome_json, measured_at, correct, prediction_id),
        )


def mark_batch_aggregate(prediction_id: int, reason: str = "Cost-Tracking, Outcomes über child-rows") -> None:
    """
    Sofortige Markierung einer Batch-Prediction als 'nicht messbar'.
    Verhindert pending-Hänger im outcome_tracker (LESSONS Bug-Story 1).
    """
    record_outcome(
        prediction_id,
        outcome={"type": "batch_aggregate", "reason": reason},
        correct=None,  # NULL = unmessbar, NICHT 0 (das wäre 'falsch')
    )


# ────────────────────────────────────────────────────────────
# READ / QUERY HELPERS
# ────────────────────────────────────────────────────────────
def get_prediction(prediction_id: int) -> Optional[PredictionRecord]:
    with connect(LEARNING_DB) as conn:
        row = conn.execute(
            "SELECT * FROM predictions WHERE id = ?", (prediction_id,)
        ).fetchone()
    return PredictionRecord(**dict(row)) if row else None


def pending_outcomes(
    job_source: Optional[str] = None,
    older_than_days: int = 7,
    limit: int = 1000,
) -> list[PredictionRecord]:
    """
    Liefert Predictions, deren Outcome noch nicht gemessen wurde.
    Wird vom outcome_tracker täglich gepollt.

    Args:
        job_source:       Filter auf bestimmte Quelle ('daily_score' etc.) oder None.
        older_than_days:  Mindest-Alter; bei daily_score normal 7d.
        limit:            Sicherheitsgrenze.
    """
    sql = """
        SELECT * FROM predictions
         WHERE outcome_correct IS NULL
           AND outcome_json IS NULL
           AND date(created_at) <= date('now', ?)
    """
    params: list[Any] = [f"-{older_than_days} day"]
    if job_source:
        sql += " AND job_source = ?"
        params.append(job_source)
    sql += " ORDER BY created_at LIMIT ?"
    params.append(limit)

    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [PredictionRecord(**dict(r)) for r in rows]


def hit_rate(
    job_source:    str,
    days:          int = 30,
    confidence:    Optional[str] = None,
    subject_id:    Optional[str] = None,
) -> dict:
    """
    Berechnet Hit-Rate über die letzten N Tage.

    Returns:
        {
          "total":     gesamte Predictions im Zeitraum,
          "measured":  Predictions mit outcome_correct ∈ {0,1},
          "correct":   davon korrekt,
          "incorrect": davon falsch,
          "pending":   noch unmessbar oder zu jung,
          "hit_rate":  correct/measured (None falls measured=0),
        }
    """
    sql_filters = ["job_source = ?", "date(created_at) >= date('now', ?)"]
    params: list[Any] = [job_source, f"-{days} day"]
    if confidence:
        sql_filters.append("confidence = ?")
        params.append(confidence)
    if subject_id:
        sql_filters.append("subject_id = ?")
        params.append(subject_id)
    where = " AND ".join(sql_filters)

    with connect(LEARNING_DB) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM predictions WHERE {where}", params
        ).fetchone()[0]
        correct = conn.execute(
            f"SELECT COUNT(*) FROM predictions WHERE {where} AND outcome_correct = 1", params
        ).fetchone()[0]
        incorrect = conn.execute(
            f"SELECT COUNT(*) FROM predictions WHERE {where} AND outcome_correct = 0", params
        ).fetchone()[0]

    measured = correct + incorrect
    pending = total - measured
    return {
        "total":     int(total),
        "measured":  int(measured),
        "correct":   int(correct),
        "incorrect": int(incorrect),
        "pending":   int(pending),
        "hit_rate":  (correct / measured) if measured > 0 else None,
    }


def hit_rate_stratified(job_source: str, days: int = 30) -> dict:
    """Hit-Rate aufgeschlüsselt nach Konfidenz-Stufe (für Meta-Review-Prompts)."""
    return {
        "overall": hit_rate(job_source, days),
        "high":    hit_rate(job_source, days, confidence="high"),
        "medium":  hit_rate(job_source, days, confidence="medium"),
        "low":     hit_rate(job_source, days, confidence="low"),
    }


def feedback_summary(days: int = 30) -> dict:
    """
    Aggregiert Telegram-Inline-Button-Feedback der letzten N Tage.
    Wird in den nächsten Sonnet-Prompt injected (LESSONS Pattern 2).
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(
            """
            SELECT feedback_type, reason_code, COUNT(*) as n
              FROM feedback_reasons
             WHERE date(created_at) >= date('now', ?)
             GROUP BY feedback_type, reason_code
             ORDER BY n DESC
            """,
            (f"-{days} day",),
        ).fetchall()
    return {"window_days": days, "by_type_reason": [dict(r) for r in rows]}


def log_feedback(
    prediction_id: int,
    feedback_type: str,
    reason_code:   Optional[str] = None,
    reason_text:   Optional[str] = None,
) -> int:
    """Telegram-Inline-Button-Click → feedback_reasons-Row."""
    with connect(LEARNING_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback_reasons (prediction_id, feedback_type, reason_code, reason_text)
            VALUES (?, ?, ?, ?)
            """,
            (prediction_id, feedback_type, reason_code, reason_text),
        )
        return int(cur.lastrowid)



def ticker_feedback_summary(ticker: str, days: int = 60) -> list[dict]:
    """
    Holt Feedback-Reasons fuer einen bestimmten Ticker.
    Joined mit predictions ueber prediction_id → subject_id.
    """
    sql = """
        SELECT fr.feedback_type, fr.reason_code, fr.reason_text, fr.created_at
          FROM feedback_reasons fr
          JOIN predictions p ON fr.prediction_id = p.id
         WHERE p.subject_id = ?
           AND fr.created_at >= datetime('now', ?)
         ORDER BY fr.created_at DESC
         LIMIT 20
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (ticker, f"-{days} day")).fetchall()
    return [dict(r) for r in rows]



def latest_risk_score_summary(days: int = 30) -> dict:
    """Aggregat-Stats fuer monthly_dca / meta_review-Prompts."""
    sql = """
        SELECT subject_id,
               AVG(json_extract(output_json, '$.composite')) AS avg_composite,
               COUNT(*) AS n
          FROM predictions
         WHERE job_source = 'daily_score'
           AND created_at >= datetime('now', ?)
         GROUP BY subject_id
         ORDER BY avg_composite ASC
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (f"-{days} day",)).fetchall()
    return [
        {"ticker": r["subject_id"], "avg_composite": float(r["avg_composite"] or 0), "n": int(r["n"])}
        for r in rows
    ]

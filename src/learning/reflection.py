"""
Reflection-Engine — Per-Ticker Outcome-Reflections fuer den Self-Learning-Loop.

Inspiriert von TradingAgents (UCLA/MIT): nach jeder Outcome-Messung wird eine
strukturierte Reflexion generiert, die analysiert:
  - Was wurde vorhergesagt (alert_level, composite, confidence)?
  - Was ist tatsaechlich passiert (return, drawdown)?
  - Welche Dimension war hauptverantwortlich (blame/credit)?
  - Was sollte beim naechsten Scoring dieses Tickers anders gewichtet werden?

Die letzten N same-ticker Reflections werden in den naechsten Score-Prompt
injiziert → der Scorer lernt aus eigenen Fehlern ohne Code-Aenderung.

Pattern: TradingAgents Decision Log → Outcome-grounded Reflections →
         Cross-Ticker-Lessons → Prompt-Injection
"""

from __future__ import annotations

import json
from typing import Optional

from ..common.json_utils import safe_parse
from ..common.storage import LEARNING_DB, connect


# ────────────────────────────────────────────────────────────
# REFLECTION GENERATION
# ────────────────────────────────────────────────────────────
def generate_reflection(
    prediction_id: int,
    ticker: str,
    alert_level: int,
    outcome_correct: Optional[int],
    outcome_data: dict,
    output_json: Optional[str] = None,
) -> Optional[int]:
    """
    Generiert eine strukturierte Reflexion fuer eine gemessene Prediction
    und speichert sie in der reflections-Tabelle.

    Args:
        prediction_id: ID der Prediction
        ticker: z.B. "NVDA"
        alert_level: 0-3 der urspruenglichen Vorhersage
        outcome_correct: 1/0/None
        outcome_data: dict mit windows, alert_level etc. aus measure_outcome_for()
        output_json: raw output_json der Prediction (fuer Dim-Analyse)

    Returns:
        reflection_id oder None falls keine Reflexion moeglich
    """
    if outcome_correct is None:
        return None  # Watch-Level oder unmessbar → keine Reflexion

    # Outcome-Daten extrahieren
    windows = outcome_data.get("windows", {})
    w7d = windows.get("7d", {})
    return_7d = w7d.get("return_pct")
    max_dd_7d = w7d.get("max_drawdown")

    w1d = windows.get("1d", {})
    return_1d = w1d.get("return_pct")

    # Dimension-Analyse aus output_json
    output = safe_parse(output_json or "{}", default={})
    dimensions = output.get("dimensions", [])
    composite = output.get("composite", 0)
    confidence = output.get("confidence", "?")

    # Dimension-Blame: welche Dim hat am meisten zum Score beigetragen?
    dimension_blame = _identify_blame(dimensions, outcome_correct)

    # Reflexions-Text generieren
    reflection_md = _build_reflection_text(
        ticker=ticker,
        alert_level=alert_level,
        composite=composite,
        confidence=confidence,
        outcome_correct=outcome_correct,
        return_1d=return_1d,
        return_7d=return_7d,
        max_dd_7d=max_dd_7d,
        dimension_blame=dimension_blame,
        dimensions=dimensions,
    )

    # In DB speichern
    with connect(LEARNING_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO reflections
                (prediction_id, ticker, alert_level, outcome_correct,
                 reflection_md, dimension_blame, return_7d, max_dd_7d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction_id, ticker, alert_level, outcome_correct,
                reflection_md,
                json.dumps(dimension_blame) if dimension_blame else None,
                return_7d, max_dd_7d,
            ),
        )
        return int(cur.lastrowid)


def _identify_blame(dimensions: list[dict], outcome_correct: int) -> Optional[dict]:
    """
    Identifiziert welche Dimension am meisten zum Ergebnis beigetragen hat.

    Bei FALSCHEM Outcome (correct=0):
      - Wenn alert_level hoch aber kein Drawdown: welche Dim hat faelschlich gefeuert?
        → Die mit dem hoechsten Score + triggered=True ist der "false alarm"-Schuldige
      - Wenn alert_level niedrig aber Drawdown kam: welche Dim haette warnen muessen?
        → Schwer zu sagen, aber wir loggen die Dims die NICHT triggered haben

    Bei RICHTIGEM Outcome (correct=1):
      - Welche Dim hat den richtigen Call gemacht?
        → Die mit dem hoechsten Score + triggered=True bekommt Credit
    """
    if not dimensions:
        return None

    triggered = [d for d in dimensions if d.get("triggered")]
    not_triggered = [d for d in dimensions if not d.get("triggered")]

    if outcome_correct == 0:
        # Falsch: entweder false alarm oder missed risk
        if triggered:
            # False alarm: hoechster Score hat faelschlich gefeuert
            worst = max(triggered, key=lambda d: d.get("score", 0))
            return {
                "type": "false_alarm",
                "dimension": worst.get("name"),
                "score": worst.get("score", 0),
                "message": f"{worst.get('name')} feuerte faelschlich (score={worst.get('score', 0):.0f})",
            }
        else:
            # Missed risk: keine Dim hat gewarnt, aber Risiko trat ein
            # Die mit dem hoechsten Score war am naechsten dran
            if dimensions:
                closest = max(dimensions, key=lambda d: d.get("score", 0))
                return {
                    "type": "missed_risk",
                    "dimension": closest.get("name"),
                    "score": closest.get("score", 0),
                    "message": f"Risiko verpasst, naechste Dim war {closest.get('name')} (score={closest.get('score', 0):.0f})",
                }
            return None
    else:
        # Richtig
        if triggered:
            best = max(triggered, key=lambda d: d.get("score", 0))
            return {
                "type": "correct_signal",
                "dimension": best.get("name"),
                "score": best.get("score", 0),
                "message": f"{best.get('name')} hat korrekt gewarnt (score={best.get('score', 0):.0f})",
            }
        else:
            return {
                "type": "correct_green",
                "dimension": None,
                "score": 0,
                "message": "Korrekt kein Alarm ausgeloest",
            }


def _build_reflection_text(
    ticker: str,
    alert_level: int,
    composite: float,
    confidence: str,
    outcome_correct: int,
    return_1d: Optional[float],
    return_7d: Optional[float],
    max_dd_7d: Optional[float],
    dimension_blame: Optional[dict],
    dimensions: list[dict],
) -> str:
    """Baut einen menschenlesbaren Reflexions-Absatz."""
    alert_labels = {0: "Green", 1: "Watch", 2: "Caution", 3: "Red"}
    alert_label = alert_labels.get(alert_level, f"Level-{alert_level}")

    verdict = "KORREKT" if outcome_correct == 1 else "FALSCH"

    parts = [f"[{ticker}] Vorhersage {alert_label} (composite={composite:.0f}, conf={confidence}) → {verdict}"]

    # Realisierte Bewegung
    moves = []
    if return_1d is not None:
        moves.append(f"1d: {return_1d:+.1%}")
    if return_7d is not None:
        moves.append(f"7d: {return_7d:+.1%}")
    if max_dd_7d is not None:
        moves.append(f"max-dd-7d: {max_dd_7d:+.1%}")
    if moves:
        parts.append(f"Realisiert: {', '.join(moves)}")

    # Blame/Credit
    if dimension_blame:
        parts.append(f"Analyse: {dimension_blame['message']}")

    # Lektion
    if outcome_correct == 0:
        if alert_level >= 2 and (max_dd_7d is None or max_dd_7d > -0.05):
            parts.append(
                f"Lektion: {ticker} wurde als riskant eingestuft aber der Drawdown blieb aus. "
                f"Bei zukuenftigen Bewertungen vorsichtiger mit hohen Scores fuer aehnliche Konstellationen."
            )
        elif alert_level <= 1 and max_dd_7d is not None and max_dd_7d <= -0.05:
            # Top triggered dims die HAETTEN warnen sollen
            missed_dims = sorted(dimensions, key=lambda d: d.get("score", 0), reverse=True)[:3]
            dim_names = [d.get("name", "?") for d in missed_dims]
            parts.append(
                f"Lektion: {ticker} wurde als sicher eingestuft aber fiel {max_dd_7d:.1%}. "
                f"Erhoehe Aufmerksamkeit fuer: {', '.join(dim_names)}."
            )
    else:
        if alert_level >= 2:
            parts.append(
                f"Lektion: Risiko-Warnung fuer {ticker} war berechtigt (DD={max_dd_7d:+.1%}). "
                f"Aehnliche Konstellationen weiterhin ernst nehmen."
                if max_dd_7d is not None else
                f"Lektion: Risiko-Warnung fuer {ticker} war berechtigt."
            )
        else:
            parts.append(
                f"Lektion: {ticker} korrekt als stabil eingestuft. "
                f"Scoring-Kalibrierung stimmt fuer diesen Ticker."
            )

    return "\n".join(parts)


# ────────────────────────────────────────────────────────────
# REFLECTION RETRIEVAL
# ────────────────────────────────────────────────────────────
def get_ticker_reflections(
    ticker: str,
    limit: int = 5,
    days: int = 90,
) -> list[dict]:
    """
    Holt die letzten N Reflections fuer einen bestimmten Ticker.
    Wird fuer Prompt-Injection in score_portfolio.py genutzt.
    """
    sql = """
        SELECT id, created_at, ticker, alert_level, outcome_correct,
               reflection_md, dimension_blame, return_7d, max_dd_7d
          FROM reflections
         WHERE ticker = ?
           AND created_at >= datetime('now', ?)
         ORDER BY created_at DESC
         LIMIT ?
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (ticker, f"-{days} day", limit)).fetchall()

    return [
        {
            "id":               row["id"],
            "created_at":       row["created_at"],
            "ticker":           row["ticker"],
            "alert_level":      row["alert_level"],
            "outcome_correct":  row["outcome_correct"],
            "reflection_md":    row["reflection_md"],
            "dimension_blame":  safe_parse(row["dimension_blame"] or "{}", default={}),
            "return_7d":        row["return_7d"],
            "max_dd_7d":        row["max_dd_7d"],
        }
        for row in rows
    ]


def get_cross_ticker_lessons(
    limit: int = 10,
    days: int = 30,
) -> list[dict]:
    """
    Holt die letzten N Reflections ueber ALLE Ticker.
    Fuer globale Lern-Patterns (Cross-Ticker-Lessons aus TradingAgents).
    """
    sql = """
        SELECT id, created_at, ticker, alert_level, outcome_correct,
               reflection_md, dimension_blame, return_7d, max_dd_7d
          FROM reflections
         WHERE created_at >= datetime('now', ?)
         ORDER BY created_at DESC
         LIMIT ?
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (f"-{days} day", limit)).fetchall()

    return [
        {
            "id":               row["id"],
            "created_at":       row["created_at"],
            "ticker":           row["ticker"],
            "alert_level":      row["alert_level"],
            "outcome_correct":  row["outcome_correct"],
            "reflection_md":    row["reflection_md"],
            "dimension_blame":  safe_parse(row["dimension_blame"] or "{}", default={}),
            "return_7d":        row["return_7d"],
            "max_dd_7d":        row["max_dd_7d"],
        }
        for row in rows
    ]


# ────────────────────────────────────────────────────────────
# PROMPT INJECTION BLOCKS
# ────────────────────────────────────────────────────────────
def ticker_reflection_block(ticker: str, limit: int = 5) -> str:
    """
    Formatiert die letzten Reflections eines Tickers als Prompt-Injection-Block.
    Wird in score_portfolio.py vor dem Scoring eines Tickers injiziert.
    """
    reflections = get_ticker_reflections(ticker, limit=limit)
    if not reflections:
        return ""

    correct = sum(1 for r in reflections if r["outcome_correct"] == 1)
    wrong = sum(1 for r in reflections if r["outcome_correct"] == 0)

    parts = [f"## Outcome-Reflections fuer {ticker} (letzte {len(reflections)}, {correct} korrekt / {wrong} falsch)"]
    for r in reflections:
        parts.append(f"  [{r['created_at'][:10]}] {r['reflection_md']}")
    parts.append("")
    return "\n".join(parts)


def global_reflection_block(limit: int = 10) -> str:
    """
    Formatiert Cross-Ticker-Reflections als Prompt-Injection-Block.
    Zeigt globale Muster: welche Dimensionen funktionieren, welche nicht.
    """
    reflections = get_cross_ticker_lessons(limit=limit)
    if not reflections:
        return ""

    # Aggregate: welche Dims tauchen in blame auf?
    dim_stats: dict[str, dict] = {}
    for r in reflections:
        blame = r.get("dimension_blame", {})
        dim = blame.get("dimension")
        btype = blame.get("type", "")
        if not dim:
            continue
        if dim not in dim_stats:
            dim_stats[dim] = {"correct_signal": 0, "false_alarm": 0, "missed_risk": 0}
        if btype in dim_stats[dim]:
            dim_stats[dim][btype] += 1

    parts = [f"## Cross-Ticker Lessons (letzte {len(reflections)} Outcomes)"]

    # Dim-Zusammenfassung
    if dim_stats:
        parts.append("Dimensions-Bilanz:")
        for dim, stats in sorted(dim_stats.items(), key=lambda x: -(x[1]["correct_signal"])):
            parts.append(
                f"  {dim}: {stats['correct_signal']} korrekte Signale, "
                f"{stats['false_alarm']} Fehlalarme, {stats['missed_risk']} verpasste Risiken"
            )
    parts.append("")

    # Letzte 5 Reflections als Kontext
    parts.append("Letzte Outcomes:")
    for r in reflections[:5]:
        verdict = "OK" if r["outcome_correct"] == 1 else "FALSCH"
        dd = f", dd={r['max_dd_7d']:+.1%}" if r["max_dd_7d"] is not None else ""
        parts.append(f"  {r['ticker']} [{r['created_at'][:10]}]: {verdict}{dd}")
    parts.append("")

    return "\n".join(parts)

#!/usr/bin/env python3
"""
meta_review.py — monatlicher Opus-Reflexion-Job.

Pipeline:
  1. Aggregiere outcomes der letzten 30d pro job_source
  2. Stratifiziere nach Konfidenz, Ticker, Strategy-Label, alert_level
  3. Aggregate feedback_reasons der letzten 30d
  4. Drift-Detection (recent 7d vs prior 7d)
  5. Opus-Call mit allem als Kontext, fordert JSON-action_plan + config_patches
  6. Schreibe reviews/<date>-<source>.md (Markdown)
  7. Schreibe meta_reviews-Tabellen-Eintrag (mit prediction_id)
  8. Validiere + logge config_patches (Audit-Trail)

Wird von:
  - invest-pi-meta-review.timer (monatlich, 2. um 04:00 CEST)
  - manuell triggerbar fuer dry-run

Cost: Opus ist teurer als Sonnet (~$15/$75 per 1M Tokens).
Ein Meta-Review-Lauf: ~3-8 EUR. Drum nur 1x/Monat.

Feature-Flag: skip wenn ANTHROPIC_API_KEY leer.

Usage:
  python scripts/meta_review.py                    # daily_score (default)
  python scripts/meta_review.py --source monthly_dca
  python scripts/meta_review.py --source trade_decision
  python scripts/meta_review.py --dry-run          # baut Prompt aber kein API-Call
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# .env laden
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts import notifier
from src.common.json_utils import safe_parse
from src.common.llm import call_opus, is_configured as llm_configured
from src.common.outcomes import detect_drift
from src.common.predictions import (
    feedback_summary,
    hit_rate,
    hit_rate_stratified,
)
from src.common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.meta_review")

REVIEWS_DIR = Path(__file__).resolve().parents[1] / "reviews"
REVIEWS_DIR.mkdir(exist_ok=True)


def _gather_context(job_source: str, days: int = 30) -> dict:
    """Sammelt allen Kontext fuer Opus."""
    rates = hit_rate_stratified(job_source, days=days)

    # Per-Strategy-Label (z.B. conservative-v1 vs moderate-v1)
    sql_strategy = """
        SELECT model AS strategy,
               COUNT(*) AS total,
               SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS correct,
               SUM(CASE WHEN outcome_correct=0 THEN 1 ELSE 0 END) AS incorrect
          FROM predictions
         WHERE job_source = ?
           AND created_at >= datetime('now', ?)
         GROUP BY model
    """
    with connect(LEARNING_DB) as conn:
        per_strat = [dict(r) for r in conn.execute(sql_strategy, (job_source, f"-{days} day")).fetchall()]

    # Per-Ticker (top 10 nach measured-count)
    sql_ticker = """
        SELECT subject_id AS ticker,
               COUNT(*) AS total,
               SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS correct,
               SUM(CASE WHEN outcome_correct=0 THEN 1 ELSE 0 END) AS incorrect
          FROM predictions
         WHERE job_source = ?
           AND subject_type = 'ticker'
           AND created_at >= datetime('now', ?)
         GROUP BY subject_id
         ORDER BY total DESC LIMIT 15
    """
    with connect(LEARNING_DB) as conn:
        per_ticker = [dict(r) for r in conn.execute(sql_ticker, (job_source, f"-{days} day")).fetchall()]

    drift = detect_drift(job_source, window_days=7) or {}
    fb = feedback_summary(days=days)

    return {
        "job_source":      job_source,
        "period_days":     days,
        "hit_rate":        rates,
        "per_strategy":    per_strat,
        "per_ticker":      per_ticker,
        "drift":           drift,
        "feedback":        fb,
    }


def _build_prompt(ctx: dict) -> tuple[str, str]:
    system = (
        "Du bist ein erfahrener Quant-Analyst der monatlich die Performance eines automatisierten "
        "Self-Learning-Investment-Systems reflektiert. Du bekommst Aggregat-Daten der letzten 30 Tage "
        "und sollst konkrete, umsetzbare Aktionen empfehlen.\n\n"
        "WICHTIG:\n"
        "1. Antworte ausschliesslich im JSON-Format (kein Prosa-Drumherum):\n"
        "{\n"
        '  "summary_md":  "<2-4 Absaetze Markdown-Reflexion>",\n'
        '  "action_plan": {\n'
        '    "prio_1": ["<aktion>", ...],\n'
        '    "prio_2": ["<aktion>", ...],\n'
        '    "prio_3": ["<aktion>", ...]\n'
        "  },\n"
        '  "config_patches": [\n'
        "    {\n"
        '      "path": "<config.pfad>",\n'
        '      "old_value": <aktueller_wert>,\n'
        '      "new_value": <neuer_wert>,\n'
        '      "reason": "<warum>"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "2. Sei spezifisch. Statt 'Schwellen anpassen': 'composite-Schwelle von 45 auf 35 senken weil "
        "moderate-v1 bei composite>40 nur 38% korrekt war'.\n\n"
        "3. Wenn du Konfidenz-Probleme erkennst (z.B. high-conf nur 50% korrekt), nenne das explizit.\n\n"
        "4. action_plan-Items werden in spaetere LLM-Prompts injected — formuliere sie als kurze "
        "Direktiven die ein anderer LLM beim naechsten Score- oder DCA-Call beachten kann.\n\n"
        "5. config_patches sind maschinenlesbare Config-Aenderungen. Erlaubte Pfade:\n"
        "   trading.stop_loss_pct (0.03-0.25), trading.take_profit_pct (0.05-0.50),\n"
        "   trading.trailing_stop_pct (0.03-0.20), trading.score_buy_max (20-60),\n"
        "   trading.max_open_positions (3-15), trading.cash_floor_pct (0.05-0.50),\n"
        "   risk_scorer.threshold_caution (30-60), risk_scorer.threshold_red (60-90).\n"
        "   Nur Patches vorschlagen wenn die Daten das klar rechtfertigen."
    )
    prompt = (
        f"## Job-Source: {ctx['job_source']}\n"
        f"## Zeitraum: letzte {ctx['period_days']}d\n\n"
        f"## Hit-Rate (Konfidenz-stratifiziert)\n"
        f"{json.dumps(ctx['hit_rate'], indent=2)}\n\n"
        f"## Per Strategy-Label\n"
        f"{json.dumps(ctx['per_strategy'], indent=2)}\n\n"
        f"## Per Ticker (Top 15)\n"
        f"{json.dumps(ctx['per_ticker'], indent=2)}\n\n"
        f"## Drift-Detection (recent 7d vs prior 7d)\n"
        f"{json.dumps(ctx['drift'], indent=2)}\n\n"
        f"## User-Feedback-Patterns\n"
        f"{json.dumps(ctx['feedback'], indent=2)}\n\n"
        "Schreibe deinen Review als JSON wie im System-Prompt beschrieben."
    )
    return system, prompt


def run(job_source: str, dry_run: bool = False) -> dict:
    if not dry_run and not llm_configured():
        log.warning("ANTHROPIC_API_KEY nicht gesetzt, meta_review skipped")
        return {"skipped": True, "reason": "no api key"}

    ctx = _gather_context(job_source)

    if ctx["hit_rate"]["overall"]["measured"] == 0:
        log.info(f"keine measured outcomes fuer {job_source}, skip review")
        return {"skipped": True, "reason": "no outcomes yet"}

    system, prompt = _build_prompt(ctx)

    if dry_run:
        return {
            "dry_run":     True,
            "system_len":  len(system),
            "prompt_len":  len(prompt),
            "context":     ctx,
        }

    result = call_opus(
        system=system,
        prompt=prompt,
        job_source="meta_review",
        subject_type="batch",
        subject_id=None,
        input_summary=f"meta-review {job_source} {ctx['period_days']}d",
        max_tokens=4096,
        temperature=0.3,
        estimated_cost_eur=4.0,
    )

    if not result.ok:
        log.error(f"opus call failed: {result.error}")
        if notifier.is_configured():
            notifier.send_info(f"<b>meta_review {job_source}</b>: {result.error or '?'}", label="meta_error")
        return {"error": result.error}

    parsed = result.parsed_json or safe_parse(result.text, default={})
    summary_md  = parsed.get("summary_md", "(keine summary)")
    action_plan = parsed.get("action_plan", {})

    # Markdown-Datei schreiben
    today = dt.datetime.utcnow().date().isoformat()
    md_path = REVIEWS_DIR / f"{today}-{job_source}.md"
    md_content = (
        f"# Meta-Review: {job_source} ({today})\n\n"
        f"_Generated by Opus, prediction_id={result.prediction_id}, cost {result.cost_eur:.4f} EUR_\n\n"
        f"## Summary\n{summary_md}\n\n"
        f"## Action Plan\n```json\n{json.dumps(action_plan, indent=2)}\n```\n"
    )
    md_path.write_text(md_content)

    # meta_reviews-Tabelle
    period_start = (dt.datetime.utcnow() - dt.timedelta(days=ctx['period_days'])).date().isoformat()
    period_end   = dt.datetime.utcnow().date().isoformat()
    with connect(LEARNING_DB) as conn:
        conn.execute(
            """
            INSERT INTO meta_reviews
                (period_start, period_end, job_source, summary_md, action_plan_js, prediction_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (period_start, period_end, job_source, summary_md,
             json.dumps(action_plan), result.prediction_id),
        )

    # Config-Patches verarbeiten
    config_patches = parsed.get("config_patches", [])
    patch_summary = ""
    if config_patches:
        try:
            from src.learning.config_patcher import log_patches
            # meta_review_id aus der gerade geschriebenen Row holen
            with connect(LEARNING_DB) as conn:
                mr_row = conn.execute(
                    "SELECT id FROM meta_reviews ORDER BY id DESC LIMIT 1"
                ).fetchone()
            mr_id = mr_row["id"] if mr_row else None
            results = log_patches(config_patches, meta_review_id=mr_id)
            accepted = [r for r in results if r.accepted]
            if accepted:
                patch_summary = f"\nConfig-Patches: {len(accepted)} akzeptiert"
                for r in accepted:
                    patch_summary += f"\n  {r.path}: {r.old_value} -> {r.new_value}"
        except Exception as e:
            log.warning(f"config patch processing failed: {e}")

    # Telegram-Push
    if notifier.is_configured():
        notifier.send_info(
            f"<b>Meta-Review fuer {job_source}</b> fertig\n"
            f"Hit-Rate: {(ctx['hit_rate']['overall']['hit_rate'] or 0):.0%} "
            f"({ctx['hit_rate']['overall']['measured']} measured)\n"
            f"Cost: {result.cost_eur:.3f} EUR\n"
            f"Datei: reviews/{md_path.name}"
            f"{patch_summary}",
            label="meta_review",
        )

    return {
        "ok":           True,
        "md_path":      str(md_path),
        "cost_eur":     result.cost_eur,
        "prediction_id": result.prediction_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="daily_score")
    parser.add_argument("--dry-run", action="store_true",
                        help="Prompt bauen, Context sammeln, kein API-Call")
    args = parser.parse_args()

    result = run(args.source, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") or result.get("dry_run") or result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main())

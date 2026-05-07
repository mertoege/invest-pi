#!/usr/bin/env python3
"""
weekly_mini_review.py — wöchentlicher Sonnet-basierter Quick-Review.

Schneller und billiger als der monatliche Opus-Review (~0.02 EUR vs ~3 EUR).
Fokus: Ticker-Probleme erkennen, schnelle Config-Patches, Drift warnen.

Bekommt auch die letzten 3 eigenen Prior-Reviews um aus eigenen Empfehlungen zu lernen.

Wird von invest-pi-weekly-mini-review.timer (So 10:00) aufgerufen.
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

env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("\'"))

from src.alerts import notifier
from src.common.json_utils import safe_parse
from src.common.llm import call_sonnet, is_configured as llm_configured
from src.common.outcomes import detect_drift
from src.common.predictions import hit_rate_stratified, feedback_summary
from src.common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.weekly_mini_review")

REVIEWS_DIR = Path(__file__).resolve().parents[1] / "reviews"
REVIEWS_DIR.mkdir(exist_ok=True)


def _prior_reviews(limit: int = 3) -> list[dict]:
    """Holt die letzten N Reviews (mini + opus) als Kontext."""
    sql = """
        SELECT created_at, job_source, summary_md, action_plan_js
          FROM meta_reviews
         ORDER BY created_at DESC
         LIMIT ?
    """
    try:
        with connect(LEARNING_DB) as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _recent_patches(days: int = 14) -> list[dict]:
    """Holt kuerzlich angewandte Patches um Effekt zu evaluieren."""
    sql = """
        SELECT path, old_value, new_value, reason, applied_at, source
          FROM config_patch_log
         WHERE accepted = 1
           AND created_at >= datetime('now', ?)
         ORDER BY created_at DESC
         LIMIT 10
    """
    try:
        with connect(LEARNING_DB) as conn:
            rows = conn.execute(sql, (f"-{days} day",)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _gather_context(job_source: str = "daily_score", days: int = 7) -> dict:
    rates = hit_rate_stratified(job_source, days=days)
    drift = detect_drift(job_source, window_days=7) or {}

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
         ORDER BY total DESC LIMIT 10
    """
    with connect(LEARNING_DB) as conn:
        per_ticker = [dict(r) for r in conn.execute(sql_ticker, (job_source, f"-{days} day")).fetchall()]

    regime_ctx = {}
    try:
        from src.learning.regime import current_regime
        from src.trading import load_trading_config
        r = current_regime()
        t_cfg = load_trading_config()
        regime_ctx = {
            "current_regime": r.label,
            "probability": r.probability,
            "active_profile": t_cfg.regime_profiles.get(r.label, {}),
        }
    except Exception:
        pass

    return {
        "job_source": job_source,
        "period_days": days,
        "hit_rate": rates,
        "per_ticker": per_ticker,
        "drift": drift,
        "regime": regime_ctx,
        "prior_reviews": _prior_reviews(3),
        "recent_patches": _recent_patches(14),
    }


def _build_prompt(ctx: dict) -> tuple[str, str]:
    system = (
        "Du bist ein Quant-Analyst der wöchentlich ein Self-Learning-Investment-System reviewt. "
        "Du bekommst 7-Tage-Daten + deine eigenen Prior-Reviews und deren Patches.\n\n"
        "WICHTIG:\n"
        "1. JSON-Format:\n"
        "{\n"
        '  "summary_md": "<1-2 Absaetze>",\n'
        '  "action_plan": {"prio_1": [...], "prio_2": [...]},\n'
        '  "config_patches": [{...}]\n'
        "}\n\n"
        "2. Wenn du Prior-Reviews siehst: bewerte ob deine letzten Empfehlungen gewirkt haben.\n"
        "3. Erlaubte config_patches Pfade:\n"
        "   trading.* (stop_loss_pct, take_profit_pct, score_buy_max, max_open_positions, etc.)\n"
        "   regime.<label>.<param> (score_buy_max, target_invest_pct, sector_avoid, etc.)\n"
        "   Nur patchen wenn 7d-Daten das klar rechtfertigen.\n"
        "4. Max 3 Patches pro Weekly-Review. Sei konservativ.\n"
        "5. Fokus: welche Ticker performen schlecht? Drift? Regime-Wechsel?"
    )

    prior_block = ""
    if ctx.get("prior_reviews"):
        prior_block = "## Deine Prior-Reviews\n"
        for pr in ctx["prior_reviews"]:
            prior_block += f"### {pr.get('created_at', '?')} ({pr.get('job_source', '?')})\n"
            prior_block += f"{pr.get('summary_md', '(keine summary)')}\n"
            if pr.get("action_plan_js"):
                try:
                    ap = json.loads(pr["action_plan_js"]) if isinstance(pr["action_plan_js"], str) else pr["action_plan_js"]
                    prior_block += f"Actions: {json.dumps(ap)}\n"
                except Exception:
                    pass
            prior_block += "\n"

    patch_block = ""
    if ctx.get("recent_patches"):
        patch_block = "## Kuerzlich angewandte Patches\n"
        for p in ctx["recent_patches"]:
            patch_block += f"- {p.get('path')}: {p.get('old_value')} -> {p.get('new_value')} ({p.get('reason')})\n"
        patch_block += "\n"

    regime_block = ""
    if ctx.get("regime"):
        regime_block = f"## Aktuelles Regime\n{json.dumps(ctx['regime'], indent=2)}\n\n"

    prompt = (
        f"## Weekly Mini-Review: {ctx['job_source']}\n"
        f"## Zeitraum: letzte {ctx['period_days']}d\n\n"
        f"## Hit-Rate\n{json.dumps(ctx['hit_rate'], indent=2)}\n\n"
        f"## Per Ticker (Top 10)\n{json.dumps(ctx['per_ticker'], indent=2)}\n\n"
        f"{regime_block}"
        f"{prior_block}"
        f"{patch_block}"
        f"## Drift\n{json.dumps(ctx['drift'], indent=2)}\n\n"
        f"Schreibe deinen Review als JSON."
    )
    return system, prompt


def run(job_source: str = "daily_score", dry_run: bool = False) -> dict:
    if not dry_run and not llm_configured():
        return {"skipped": True, "reason": "no api key"}

    ctx = _gather_context(job_source, days=7)

    if ctx["hit_rate"]["overall"]["measured"] == 0:
        return {"skipped": True, "reason": "no measured outcomes this week"}

    system, prompt = _build_prompt(ctx)

    if dry_run:
        return {"dry_run": True, "system_len": len(system), "prompt_len": len(prompt), "context": ctx}

    result = call_sonnet(
        system=system,
        prompt=prompt,
        job_source="weekly_mini_review",
        subject_type="batch",
        subject_id=None,
        input_summary=f"weekly-mini {job_source} 7d",
        max_tokens=2048,
        temperature=0.3,
        estimated_cost_eur=0.03,
    )

    if not result.ok:
        return {"error": result.error}

    parsed = result.parsed_json or safe_parse(result.text, default={})
    summary_md = parsed.get("summary_md", "(keine summary)")
    action_plan = parsed.get("action_plan", {})

    today = dt.datetime.utcnow().date().isoformat()
    md_path = REVIEWS_DIR / f"{today}-weekly_mini-{job_source}.md"
    md_content = (
        f"# Weekly Mini-Review: {job_source} ({today})\n\n"
        f"_Sonnet, cost {result.cost_eur:.4f} EUR_\n\n"
        f"## Summary\n{summary_md}\n\n"
        f"## Action Plan\n```json\n{json.dumps(action_plan, indent=2)}\n```\n"
    )
    md_path.write_text(md_content)

    with connect(LEARNING_DB) as conn:
        period_start = (dt.datetime.utcnow() - dt.timedelta(days=7)).date().isoformat()
        conn.execute(
            """INSERT INTO meta_reviews
                (period_start, period_end, job_source, summary_md, action_plan_js, prediction_id)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (period_start, today, f"weekly_mini_{job_source}", summary_md,
             json.dumps(action_plan), result.prediction_id),
        )

    config_patches = parsed.get("config_patches", [])
    if config_patches:
        try:
            from src.learning.config_patcher import log_patches
            with connect(LEARNING_DB) as conn:
                mr_row = conn.execute("SELECT id FROM meta_reviews ORDER BY id DESC LIMIT 1").fetchone()
            mr_id = mr_row["id"] if mr_row else None
            log_patches(config_patches[:3], meta_review_id=mr_id, source="weekly_mini_review")
        except Exception as e:
            log.warning(f"weekly patch processing failed: {e}")

    if notifier.is_configured():
        notifier.send_info(
            f"<b>Weekly Mini-Review</b>\n"
            f"Hit-Rate 7d: {(ctx['hit_rate']['overall']['hit_rate'] or 0):.0%}\n"
            f"Cost: {result.cost_eur:.3f} EUR",
            label="weekly_mini_review",
        )

    return {"ok": True, "md_path": str(md_path), "cost_eur": result.cost_eur}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="daily_score")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(args.source, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") or result.get("dry_run") or result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main())

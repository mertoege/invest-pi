"""
Alert-Dispatch-Layer.

Verbindet die predictions-Tabelle (lear‌ning.db) mit dem Telegram-Notifier:
  - Findet frische Stufe-2/3-Alerts seit dem letzten Dispatch
  - Schickt sie via notifier.send_alert
  - Schreibt eine notifications-Row als de-dup-Marker

Wird am Ende von score_portfolio.py aufgerufen. Idempotent: ein zweiter
Aufruf direkt danach pusht NICHTS, weil alle Alerts schon notification-rows
haben.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml

from ..common.json_utils import safe_parse
from ..common.storage import ALERTS_DB, LEARNING_DB, connect
from . import notifier

log = logging.getLogger("invest_pi.dispatch")


def _telegram_min_level() -> int:
    try:
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "config.yaml").read_text())
        return int((cfg or {}).get("settings", {}).get("telegram_min_level", 2))
    except Exception:
        return 2


def _new_alerts(min_level: int, lookback_hours: int = 6) -> list[dict]:
    """
    Holt predictions mit alert_level >= min_level aus den letzten N Stunden,
    fuer die noch keine notifications-Row existiert.
    """
    sql_predictions = """
        SELECT id, subject_id, output_json, created_at, confidence
          FROM predictions
         WHERE job_source = 'daily_score'
           AND created_at >= datetime('now', ?)
         ORDER BY created_at DESC
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql_predictions, (f"-{lookback_hours} hour",)).fetchall()

    candidates = []
    for r in rows:
        out = safe_parse(r["output_json"] or "{}", default={})
        level = int(out.get("alert_level", 0))
        if level < min_level:
            continue
        candidates.append({
            "prediction_id":  r["id"],
            "ticker":         r["subject_id"],
            "alert_level":    level,
            "composite":      float(out.get("composite", 0)),
            "triggered_dims": out.get("triggered_dims", []),
            "confidence":     r["confidence"],
            "created_at":     r["created_at"],
        })

    if not candidates:
        return []

    # Filter: schon notified?
    pred_ids = [c["prediction_id"] for c in candidates]
    placeholders = ",".join("?" * len(pred_ids))
    with connect(ALERTS_DB) as conn:
        seen = {
            r["prediction_id"]
            for r in conn.execute(
                f"SELECT prediction_id FROM notifications "
                f"WHERE channel='telegram' AND delivered=1 "
                f"AND prediction_id IN ({placeholders})",
                pred_ids,
            ).fetchall()
        }
    return [c for c in candidates if c["prediction_id"] not in seen]


def dispatch_new_alerts(
    min_level: Optional[int] = None,
    lookback_hours: int = 6,
) -> dict:
    """
    Pusht alle neuen Stufe>=min_level Alerts an Telegram. Returns Stats.
    """
    if min_level is None:
        min_level = _telegram_min_level()
    if not notifier.is_configured():
        log.info("telegram nicht konfiguriert, dispatch skipped")
        return {"skipped": True, "reason": "telegram not configured", "sent": 0}

    pending = _new_alerts(min_level, lookback_hours)
    sent, failed = 0, 0
    for a in pending:
        ok = notifier.send_alert(
            ticker=a["ticker"],
            alert_level=a["alert_level"],
            composite=a["composite"],
            triggered_dims=a["triggered_dims"],
            prediction_id=a["prediction_id"],
            dimensions_summary=f"<i>Konfidenz: {a['confidence'] or '?'}</i>",
        )
        if ok:
            sent += 1
        else:
            failed += 1

    return {
        "skipped":   False,
        "candidates": len(pending),
        "sent":       sent,
        "failed":     failed,
        "min_level":  min_level,
    }


if __name__ == "__main__":
    print(dispatch_new_alerts())

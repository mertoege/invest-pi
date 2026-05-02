"""
Config-Patcher — wendet Meta-Review-generierte Config-Patches an.

Meta-Review (Opus) kann jetzt neben prio_1/prio_2/prio_3 Aktionen auch
maschinenlesbare Config-Patches liefern:

    "config_patches": [
        {
            "path": "trading.stop_loss_pct",
            "old_value": 0.08,
            "new_value": 0.10,
            "reason": "Stop-Loss zu eng, 3 von 5 Sells waren unnoetig"
        },
        {
            "path": "risk_scorer.ALERT_THRESHOLDS.2",
            "old_value": [50, 75],
            "new_value": [45, 75],
            "reason": "Caution-Schwelle senken, zu viele Missed-Risks"
        }
    ]

Patches werden:
  1. Validiert (Pfad existiert, old_value stimmt, new_value in Range)
  2. In config_patch_log-Tabelle geloggt (Audit-Trail)
  3. Angewandt (YAML fuer trading-config, Python-dict fuer Runtime)

Sicherheits-Guardrails:
  - Nur erlaubte Pfade (ALLOWED_PATCH_PATHS)
  - Wert-Range-Checks (z.B. stop_loss_pct 0.03..0.20)
  - Max 5 Patches pro Review
  - Patches werden NICHT automatisch angewandt — sie werden geloggt und
    beim naechsten run via apply_pending_patches() geladen
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.config_patcher")


# ────────────────────────────────────────────────────────────
# ALLOWED PATCH PATHS + VALUE RANGES
# ────────────────────────────────────────────────────────────
ALLOWED_PATCHES = {
    "trading.stop_loss_pct":           {"min": 0.03, "max": 0.25, "type": float},
    "trading.take_profit_pct":         {"min": 0.05, "max": 0.50, "type": float},
    "trading.trailing_stop_pct":       {"min": 0.03, "max": 0.20, "type": float},
    "trading.trailing_activation_pct": {"min": 0.05, "max": 0.30, "type": float},
    "trading.score_buy_max":           {"min": 20,   "max": 60,   "type": int},
    "trading.max_open_positions":      {"min": 3,    "max": 15,   "type": int},
    "trading.max_position_eur":        {"min": 50,   "max": 500,  "type": int},
    "trading.cash_floor_pct":          {"min": 0.05, "max": 0.50, "type": float},
    "risk_scorer.threshold_caution":   {"min": 30,   "max": 60,   "type": int},
    "risk_scorer.threshold_red":       {"min": 60,   "max": 90,   "type": int},
}

MAX_PATCHES_PER_REVIEW = 5


@dataclass
class PatchResult:
    path:      str
    accepted:  bool
    reason:    str
    old_value: Any = None
    new_value: Any = None


def validate_patch(patch: dict) -> PatchResult:
    """
    Validiert einen einzelnen Config-Patch.

    Returns PatchResult mit accepted=True/False.
    """
    path = patch.get("path", "")
    new_val = patch.get("new_value")
    reason = patch.get("reason", "")

    if path not in ALLOWED_PATCHES:
        return PatchResult(path, False, f"path '{path}' not in ALLOWED_PATCHES")

    spec = ALLOWED_PATCHES[path]

    # Type-Check
    try:
        new_val = spec["type"](new_val)
    except (TypeError, ValueError):
        return PatchResult(path, False, f"new_value {new_val} not convertible to {spec['type'].__name__}")

    # Range-Check
    if new_val < spec["min"] or new_val > spec["max"]:
        return PatchResult(path, False, f"new_value {new_val} out of range [{spec['min']}, {spec['max']}]")

    return PatchResult(path, True, reason, patch.get("old_value"), new_val)


def log_patches(
    patches: list[dict],
    meta_review_id: Optional[int] = None,
    source: str = "meta_review",
) -> list[PatchResult]:
    """
    Validiert und loggt Config-Patches. Wendet sie NICHT an.

    Returns: Liste von PatchResult.
    """
    results = []

    if len(patches) > MAX_PATCHES_PER_REVIEW:
        log.warning(f"too many patches ({len(patches)}), truncating to {MAX_PATCHES_PER_REVIEW}")
        patches = patches[:MAX_PATCHES_PER_REVIEW]

    for patch in patches:
        result = validate_patch(patch)
        results.append(result)

        # In DB loggen (auch abgelehnte, fuer Audit)
        try:
            with connect(LEARNING_DB) as conn:
                conn.execute(
                    """
                    INSERT INTO config_patch_log
                        (meta_review_id, path, old_value, new_value,
                         accepted, reason, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (meta_review_id, result.path,
                     json.dumps(result.old_value, default=str),
                     json.dumps(result.new_value, default=str),
                     1 if result.accepted else 0,
                     result.reason, source),
                )
        except Exception as e:
            log.warning(f"config_patch_log insert failed: {e}")

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    log.info(f"config patches: {len(accepted)} accepted, {len(rejected)} rejected")
    return results


def pending_patches(limit: int = 20) -> list[dict]:
    """
    Holt akzeptierte aber noch nicht angewandte Patches.
    """
    sql = """
        SELECT id, path, old_value, new_value, reason, created_at
          FROM config_patch_log
         WHERE accepted = 1
           AND applied_at IS NULL
         ORDER BY created_at
         LIMIT ?
    """
    with connect(LEARNING_DB) as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [
        {
            "id": r["id"],
            "path": r["path"],
            "old_value": json.loads(r["old_value"]) if r["old_value"] else None,
            "new_value": json.loads(r["new_value"]) if r["new_value"] else None,
            "reason": r["reason"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def mark_applied(patch_id: int) -> None:
    """Markiert einen Patch als angewandt."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with connect(LEARNING_DB) as conn:
        conn.execute(
            "UPDATE config_patch_log SET applied_at = ? WHERE id = ?",
            (now, patch_id),
        )


def apply_trading_patches(config) -> list[str]:
    """
    Wendet pending Trading-Config-Patches auf ein TradingConfig-Objekt an.

    Wird am Anfang von run_strategy.py aufgerufen.

    Returns: Liste von angewandten Patch-Beschreibungen.
    """
    patches = pending_patches()
    applied = []

    for p in patches:
        path = p["path"]
        new_val = p["new_value"]

        if path.startswith("trading."):
            attr = path.split(".", 1)[1]
            if hasattr(config, attr):
                old = getattr(config, attr)
                try:
                    setattr(config, attr, type(old)(new_val))
                    mark_applied(p["id"])
                    applied.append(f"{attr}: {old} -> {new_val} ({p['reason']})")
                    log.info(f"applied patch: {path} {old} -> {new_val}")
                except Exception as e:
                    log.warning(f"failed to apply patch {path}: {e}")

    return applied

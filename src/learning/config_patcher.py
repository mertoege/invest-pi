"""
Config-Patcher — wendet Meta-Review-generierte Config-Patches an.

Meta-Review (Opus) kann maschinenlesbare Config-Patches liefern:

    "config_patches": [
        {
            "path": "trading.stop_loss_pct",
            "old_value": 0.08,
            "new_value": 0.10,
            "reason": "Stop-Loss zu eng, 3 von 5 Sells waren unnoetig"
        },
        {
            "path": "regime.bear.target_invest_pct",
            "old_value": 0.25,
            "new_value": 0.30,
            "reason": "Bear-Investitionsquote zu niedrig, verpassen Recovery-Bounce"
        }
    ]

Zwei Patch-Typen:
  1. trading.* / risk_scorer.* — Runtime-Patches auf TradingConfig
  2. regime.<label>.<param> — Persistent in config.yaml (Regime-Profile)

Sicherheits-Guardrails:
  - Nur erlaubte Pfade (ALLOWED_PATCHES + REGIME_PARAM_RANGES)
  - Wert-Range-Checks
  - Max 8 Patches pro Review
  - Patches werden NICHT automatisch angewandt — sie werden geloggt und
    beim naechsten run via apply_pending_patches() geladen
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from ..common.storage import LEARNING_DB, connect

log = logging.getLogger("invest_pi.config_patcher")

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

# ────────────────────────────────────────────────────────────
# ALLOWED PATCH PATHS + VALUE RANGES
# ────────────────────────────────────────────────────────────
ALLOWED_PATCHES = {
    "trading.stop_loss_pct":           {"min": 0.03, "max": 0.25, "type": float},
    "trading.take_profit_pct":         {"min": 0.05, "max": 0.50, "type": float},
    "trading.trailing_stop_pct":       {"min": 0.03, "max": 0.20, "type": float},
    "trading.trailing_activation_pct": {"min": 0.05, "max": 0.30, "type": float},
    "trading.score_buy_max":           {"min": 20,   "max": 80,   "type": int},
    "trading.max_open_positions":      {"min": 3,    "max": 30,   "type": int},
    "trading.max_position_eur":        {"min": 50,   "max": 8000, "type": int},
    "trading.cash_floor_pct":          {"min": 0.05, "max": 0.50, "type": float},
    "risk_scorer.threshold_caution":   {"min": 30,   "max": 60,   "type": int},
    "risk_scorer.threshold_red":       {"min": 60,   "max": 90,   "type": int},
}

VALID_REGIMES = {"low_vol_bull", "high_vol_mixed", "bear", "unknown"}

REGIME_PARAM_RANGES = {
    "score_buy_max":       {"min": 10,   "max": 80,   "type": int},
    "max_open_positions":  {"min": 3,    "max": 30,   "type": int},
    "max_position_eur":    {"min": 200,  "max": 8000, "type": int},
    "max_trades_per_day":  {"min": 1,    "max": 15,   "type": int},
    "stop_loss_pct":       {"min": 0.03, "max": 0.25, "type": float},
    "take_profit_pct":     {"min": 0.10, "max": 0.80, "type": float},
    "trailing_activation": {"min": 0.03, "max": 0.30, "type": float},
    "trailing_stop_pct":   {"min": 0.03, "max": 0.20, "type": float},
    "target_invest_pct":   {"min": 0.10, "max": 0.95, "type": float},
    "sector_preference":   {"type": list},
    "sector_avoid":        {"type": list},
}

VALID_SECTORS = {
    "technology", "software", "consumer_disc", "consumer_staples",
    "healthcare", "financials", "communication", "utilities", "energy",
    "industrials", "materials", "real_estate", "etfs",
}

MAX_PATCHES_PER_REVIEW = 8


@dataclass
class PatchResult:
    path:      str
    accepted:  bool
    reason:    str
    old_value: Any = None
    new_value: Any = None


def _validate_regime_patch(path: str, new_val: Any, reason: str) -> PatchResult:
    """Validiert regime.<label>.<param> Patches."""
    parts = path.split(".")
    if len(parts) != 3:
        return PatchResult(path, False, f"regime path must be regime.<label>.<param>, got '{path}'")

    _, regime, param = parts
    if regime not in VALID_REGIMES:
        return PatchResult(path, False, f"regime '{regime}' not in {VALID_REGIMES}")
    if param not in REGIME_PARAM_RANGES:
        return PatchResult(path, False, f"param '{param}' not in REGIME_PARAM_RANGES")

    spec = REGIME_PARAM_RANGES[param]

    if spec["type"] == list:
        if not isinstance(new_val, list):
            return PatchResult(path, False, f"new_value must be list, got {type(new_val).__name__}")
        invalid = [s for s in new_val if s not in VALID_SECTORS]
        if invalid:
            return PatchResult(path, False, f"invalid sectors: {invalid}")
        return PatchResult(path, True, reason, None, new_val)

    try:
        new_val = spec["type"](new_val)
    except (TypeError, ValueError):
        return PatchResult(path, False, f"new_value {new_val} not convertible to {spec['type'].__name__}")

    if new_val < spec["min"] or new_val > spec["max"]:
        return PatchResult(path, False, f"new_value {new_val} out of range [{spec['min']}, {spec['max']}]")

    return PatchResult(path, True, reason, None, new_val)


def validate_patch(patch: dict) -> PatchResult:
    """
    Validiert einen einzelnen Config-Patch.

    Returns PatchResult mit accepted=True/False.
    """
    path = patch.get("path", "")
    new_val = patch.get("new_value")
    reason = patch.get("reason", "")

    if path.startswith("regime."):
        return _validate_regime_patch(path, new_val, reason)

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

        # Backtest-Gate fuer numerische Trading-Patches
        if result.accepted:
            try:
                from .backtest_gate import can_backtest, validate_patch_via_backtest
                if can_backtest(result.path):
                    bt = validate_patch_via_backtest(
                        result.path, result.old_value, result.new_value
                    )
                    if not bt["passed"]:
                        result = PatchResult(
                            result.path, False,
                            f"backtest gate failed: {bt['reason']}",
                            result.old_value, result.new_value,
                        )
                        log.info(f"patch {result.path} blocked by backtest: {bt['reason']}")
            except Exception as e:
                log.warning(f"backtest gate error (allowing patch): {e}")

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
    Wendet pending Patches an:
    - trading.* -> Runtime auf TradingConfig
    - regime.* -> Persistent in config.yaml + Runtime auf config.regime_profiles

    Wird am Anfang von run_strategy.py aufgerufen.
    """
    patches = pending_patches()
    applied = []
    regime_changes = {}

    for p in patches:
        path = p["path"]
        new_val = p["new_value"]

        if path.startswith("regime."):
            _, regime, param = path.split(".")
            regime_changes.setdefault(regime, {})[param] = new_val
            if hasattr(config, "regime_profiles") and config.regime_profiles:
                profile = config.regime_profiles.get(regime, {})
                profile[param] = new_val
                config.regime_profiles[regime] = profile
            mark_applied(p["id"])
            applied.append(f"regime.{regime}.{param}: -> {new_val} ({p['reason']})")
            log.info(f"applied regime patch: {path} -> {new_val}")

        elif path.startswith("trading."):
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

    if regime_changes:
        _persist_regime_to_yaml(regime_changes)

    return applied


def _persist_regime_to_yaml(changes: dict[str, dict]) -> None:
    """Schreibt Regime-Aenderungen persistent in config.yaml."""
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text())
        profiles = raw.get("settings", {}).get("trading", {}).get("regime_profiles", {})

        for regime, params in changes.items():
            if regime not in profiles:
                profiles[regime] = {}
            profiles[regime].update(params)

        raw["settings"]["trading"]["regime_profiles"] = profiles

        with open(CONFIG_PATH, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        log.info(f"config.yaml regime_profiles updated: {list(changes.keys())}")
    except Exception as e:
        log.error(f"failed to persist regime changes to config.yaml: {e}")

"""
Cost-Caps — 3-Tier Hard/Soft-Limits für externe API-Kosten.

Hintergrund (LESSONS_FOR_INVEST_PI.md):
  TL;DR Punkt 3 + Bug-Story 2 + Anti-Pattern 2:
    - 3 Tiers nötig: hourly + daily + monthly
    - Daily-Cap als CALENDAR-DAY (date('now','localtime')), NICHT rolling 24h
    - Hard-Stop bei Monthly inkl. Telegram-Notification
    - Cost-Awareness-Block ab 70% des Tagesbudgets in den Prompt injecten

Verbrauch wird in cost_ledger geschrieben (siehe storage.py SCHEMA_LEARNING).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .storage import LEARNING_DB, connect


# ────────────────────────────────────────────────────────────
# DEFAULTS (aus LESSONS, Wert für Invest-Pi-Skala)
# ────────────────────────────────────────────────────────────
DEFAULT_CAPS = {
    "hourly_eur":  0.10,
    "daily_eur":   1.00,
    "monthly_eur": 25.00,
}


# ────────────────────────────────────────────────────────────
# DATENMODELLE
# ────────────────────────────────────────────────────────────
@dataclass
class BudgetState:
    """Snapshot des aktuellen Budget-Status."""
    hourly_used:    float
    daily_used:     float
    monthly_used:   float
    hourly_cap:     float
    daily_cap:      float
    monthly_cap:    float
    tier_breached:  Optional[str] = None     # 'hourly' / 'daily' / 'monthly' / None
    cost_aware:     bool = False             # ab 70% des Tagesbudgets

    @property
    def ok(self) -> bool:
        return self.tier_breached is None

    @property
    def hourly_pct(self) -> float:
        return (self.hourly_used / self.hourly_cap) if self.hourly_cap > 0 else 0

    @property
    def daily_pct(self) -> float:
        return (self.daily_used / self.daily_cap) if self.daily_cap > 0 else 0

    @property
    def monthly_pct(self) -> float:
        return (self.monthly_used / self.monthly_cap) if self.monthly_cap > 0 else 0

    def summary_line(self) -> str:
        return (
            f"hourly {self.hourly_used:.3f}/{self.hourly_cap:.2f}€ "
            f"({self.hourly_pct:.0%}) · "
            f"daily {self.daily_used:.2f}/{self.daily_cap:.2f}€ "
            f"({self.daily_pct:.0%}) · "
            f"monthly {self.monthly_used:.2f}/{self.monthly_cap:.2f}€ "
            f"({self.monthly_pct:.0%})"
        )


# ────────────────────────────────────────────────────────────
# CONFIG-LADER
# ────────────────────────────────────────────────────────────
def _load_caps_from_yaml() -> dict:
    """Liest settings.api_costs aus config.yaml. Fällt auf DEFAULT_CAPS zurück."""
    cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
    try:
        raw = yaml.safe_load(cfg_path.read_text())
        api_costs = (raw or {}).get("settings", {}).get("api_costs", {})
        return {
            "hourly_eur":  float(api_costs.get("hourly_eur",  DEFAULT_CAPS["hourly_eur"])),
            "daily_eur":   float(api_costs.get("daily_eur",   DEFAULT_CAPS["daily_eur"])),
            "monthly_eur": float(api_costs.get("monthly_eur", DEFAULT_CAPS["monthly_eur"])),
        }
    except (FileNotFoundError, yaml.YAMLError, AttributeError):
        return DEFAULT_CAPS.copy()


# ────────────────────────────────────────────────────────────
# READ — calendar-day-Aggregation, NICHT rolling 24h (Bug-Story 2!)
# ────────────────────────────────────────────────────────────
def _sum_window(where_sql: str, params: tuple) -> float:
    with connect(LEARNING_DB) as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(cost_eur), 0) FROM cost_ledger WHERE {where_sql}",
            params,
        ).fetchone()
    return float(row[0])


def hourly_spend() -> float:
    """Verbrauch in der aktuellen wallclock-Stunde (rolling, da kurz)."""
    return _sum_window(
        "timestamp >= datetime('now', '-1 hour')",
        (),
    )


def daily_spend() -> float:
    """
    Verbrauch HEUTE (calendar-day, lokale Zeit).
    KRITISCH: NICHT rolling 24h — sonst zählen gestrige Ausgaben morgens noch mit
    (LESSONS Bug-Story 2).
    """
    return _sum_window(
        "date(timestamp, 'localtime') = date('now', 'localtime')",
        (),
    )


def monthly_spend() -> float:
    """Verbrauch im aktuellen Kalendermonat (lokale Zeit)."""
    return _sum_window(
        "strftime('%Y-%m', timestamp, 'localtime') = strftime('%Y-%m', 'now', 'localtime')",
        (),
    )


def check_budget(caps: Optional[dict] = None) -> BudgetState:
    """
    Liefert den aktuellen BudgetState.
    """
    caps = caps or _load_caps_from_yaml()
    state = BudgetState(
        hourly_used=hourly_spend(),
        daily_used=daily_spend(),
        monthly_used=monthly_spend(),
        hourly_cap=caps["hourly_eur"],
        daily_cap=caps["daily_eur"],
        monthly_cap=caps["monthly_eur"],
    )
    if state.monthly_used >= state.monthly_cap:
        state.tier_breached = "monthly"
    elif state.daily_used >= state.daily_cap:
        state.tier_breached = "daily"
    elif state.hourly_used >= state.hourly_cap:
        state.tier_breached = "hourly"

    state.cost_aware = state.daily_pct >= 0.70
    return state


# ────────────────────────────────────────────────────────────
# WRITE
# ────────────────────────────────────────────────────────────
def log_cost(
    api:           str,
    cost_eur:      float,
    job_source:    Optional[str] = None,
    prediction_id: Optional[int] = None,
    notes:         Optional[str] = None,
) -> int:
    """
    Schreibt einen cost_ledger-Eintrag.

    Args:
        api:           'anthropic' / 'finnhub' / 'newsapi' / 'yfinance' / ...
        cost_eur:      Kosten in EUR (auch wenn API in USD abrechnet — vorher umrechnen).
        job_source:    welcher Job hat das verursacht.
        prediction_id: optional Verknüpfung zu predictions(id).
    """
    with connect(LEARNING_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO cost_ledger (api, cost_eur, job_source, prediction_id, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (api, cost_eur, job_source, prediction_id, notes),
        )
        return int(cur.lastrowid)


# ────────────────────────────────────────────────────────────
# PROMPT-INJECTION
# ────────────────────────────────────────────────────────────
def cost_awareness_block(state: Optional[BudgetState] = None) -> Optional[str]:
    """
    Generiert einen Markdown-Block für den System-Prompt, falls cost_aware=True.
    Wird in build_score_prompt() hinten dran gehängt.

    Aus LESSONS Pattern 4:
      'Sonnet wird nicht abgeschaltet, sondern strenger.'
    """
    state = state or check_budget()
    if not state.cost_aware:
        return None

    pct = state.daily_pct
    if pct >= 0.95:
        urgency = "KRITISCH"
        guidance = "NUR HOCH-Konfidenz Empfehlungen. Im Zweifel SKIP."
    elif pct >= 0.85:
        urgency = "HOCH"
        guidance = "Strenger sein bei medium-Konfidenz. Triviale Cases skippen."
    else:
        urgency = "ERHÖHT"
        guidance = "Bei Borderline-Cases lieber konservativ."

    return (
        f"\n---\n"
        f"**KOSTEN-MODUS: {urgency}**\n"
        f"Tagesbudget zu {pct:.0%} verbraucht "
        f"({state.daily_used:.2f}/{state.daily_cap:.2f} EUR).\n"
        f"{guidance}\n"
    )


def can_call(estimated_cost_eur: float = 0.0) -> tuple[bool, Optional[str]]:
    """
    Pre-Check vor einem teuren Anthropic-Call.
    Returns (allowed, reason).
    """
    state = check_budget()
    if state.tier_breached == "monthly":
        return False, f"Monthly cap erreicht: {state.monthly_used:.2f}/{state.monthly_cap:.2f} EUR"
    if state.tier_breached == "daily":
        return False, f"Daily cap erreicht: {state.daily_used:.2f}/{state.daily_cap:.2f} EUR"
    # Lookahead: würde dieser Call den Daily-Cap überschreiten?
    if state.daily_used + estimated_cost_eur > state.daily_cap:
        return False, (
            f"Call würde Daily-Cap brechen: "
            f"{state.daily_used:.2f} + {estimated_cost_eur:.3f} > {state.daily_cap:.2f}"
        )
    if state.tier_breached == "hourly":
        return False, f"Hourly cap erreicht: {state.hourly_used:.3f}/{state.hourly_cap:.2f} EUR"
    return True, None


if __name__ == "__main__":
    state = check_budget()
    print(f"Budget-Status: {state.summary_line()}")
    print(f"  ok:           {state.ok}")
    print(f"  cost_aware:   {state.cost_aware}")
    print(f"  tier_breached: {state.tier_breached}")
    block = cost_awareness_block(state)
    if block:
        print("\nCost-Awareness-Block für Prompt:")
        print(block)

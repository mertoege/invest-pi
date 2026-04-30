"""
Decision-Engine — Conservative-Strategy.

Schaut auf den letzten Risk-Score eines Tickers und entscheidet:
  buy / sell / skip + Begruendung.

Conservative-Default-Regeln:
  BUY  wenn   composite < score_buy_max (default 25)
              UND triggered_n == 0 (keine aktiven Risiko-Signale)
              UND ticker NICHT in offenen Positionen
              UND ring in tradeable_rings (default [1, 2])
              UND confidence in {high, medium} (low wird konservativ gefiltert)

  SKIP wenn   alert_level >= force_skip_alert_min (default 2)
              ODER ticker bereits gehalten
              ODER max_open_positions erreicht
              ODER ring nicht handelbar

  SELL = Stop-Loss (in src/risk/limits.check_stop_loss). Hier nicht.

Jede Entscheidung wird als prediction-Row mit job_source='trade_decision' geloggt,
damit spaeter outcome-tracking + meta-review messen koennen, ob die Entscheidung
gut war.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..common.json_utils import safe_parse
from ..common.predictions import log_prediction
from ..learning.regime import regime_buy_multiplier
from ..common.storage import LEARNING_DB, connect
from . import TradingConfig


@dataclass
class TradeDecision:
    ticker:         str
    action:         str        # 'buy' | 'sell' | 'skip'
    reason:         str
    target_eur:     float = 0.0
    confidence:     str = "low"
    risk_composite: float = 0.0
    alert_level:    int = 0
    strategy_label: str = "mid_term"   # mid_term | long_term
    based_on_pred_id: Optional[int] = None     # source risk-score
    decision_pred_id: Optional[int] = None     # this decision's prediction-row
    extras:         dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────
# QUERIES
# ────────────────────────────────────────────────────────────
def latest_risk_score(ticker: str, max_age_hours: int = 24) -> Optional[dict]:
    """
    Holt die juengste daily_score-Prediction fuer den Ticker.
    Returns dict mit {pred_id, composite, alert_level, confidence, triggered_n, age_hours} or None.
    """
    sql = """
        SELECT id, output_json, confidence, created_at
          FROM predictions
         WHERE job_source = 'daily_score'
           AND subject_id = ?
         ORDER BY created_at DESC
         LIMIT 1
    """
    with connect(LEARNING_DB) as conn:
        row = conn.execute(sql, (ticker,)).fetchone()
    if not row:
        return None
    output = safe_parse(row["output_json"] or "{}", default={})
    return {
        "pred_id":      row["id"],
        "created_at":   row["created_at"],
        "composite":    float(output.get("composite", 0.0)),
        "alert_level":  int(output.get("alert_level", 0)),
        "triggered_n":  int(output.get("triggered_n", 0)),
        "confidence":   row["confidence"] or "low",
    }


# ────────────────────────────────────────────────────────────
# DECISION CORE
# ────────────────────────────────────────────────────────────
def decide_action(
    ticker:           str,
    held_tickers:     set[str],
    open_positions_count: int,
    ring:             int,
    config:           TradingConfig,
) -> TradeDecision:
    """
    Conservative-Strategy. Pure-Function, kein Network, kein DB-Write.
    Logging passiert in apply_and_log_decision.
    """
    # Hard-Filter: ist der Ring ueberhaupt tradeable?
    if ring not in config.tradeable_rings:
        return TradeDecision(
            ticker=ticker, action="skip",
            reason=f"ring {ring} not in tradeable_rings {config.tradeable_rings}",
        )

    # Hard-Filter: schon gehalten?
    if ticker in held_tickers:
        return TradeDecision(
            ticker=ticker, action="skip",
            reason="already in positions",
        )

    # Hard-Filter: zu viele offene Positionen?
    if open_positions_count >= config.max_open_positions:
        return TradeDecision(
            ticker=ticker, action="skip",
            reason=f"max_open_positions reached ({open_positions_count}/{config.max_open_positions})",
        )

    # Risk-Score holen
    score = latest_risk_score(ticker)
    if not score:
        return TradeDecision(
            ticker=ticker, action="skip",
            reason="no recent risk score",
        )

    risk = score["composite"]
    alert = score["alert_level"]
    triggered = score["triggered_n"]
    confidence = score["confidence"]

    # Hard-Filter: hohes Alert-Level (immer)
    if alert >= config.force_skip_alert_min:
        return TradeDecision(
            ticker=ticker, action="skip",
            reason=f"alert_level {alert} >= force_skip {config.force_skip_alert_min}",
            risk_composite=risk, alert_level=alert,
            confidence=confidence,
            based_on_pred_id=score["pred_id"],
        )

    # Mode-spezifische Schwellen
    is_moderate = config.mode == "moderate"
    is_experimental = config.mode == "experimental"
    alert_max = config.moderate_alert_max if (is_moderate or is_experimental) else 0

    # Konfidenz-Filter: nur conservative skipt low-confidence
    if confidence == "low" and not (is_moderate or is_experimental):
        return TradeDecision(
            ticker=ticker, action="skip",
            reason=f"confidence low; conservative skip",
            risk_composite=risk, alert_level=alert,
            confidence=confidence,
            based_on_pred_id=score["pred_id"],
        )

    # Alert-Level-Filter mode-aware
    if alert > alert_max:
        return TradeDecision(
            ticker=ticker, action="skip",
            reason=f"alert_level {alert} > mode-max {alert_max} ({config.mode})",
            risk_composite=risk, alert_level=alert,
            confidence=confidence,
            based_on_pred_id=score["pred_id"],
        )

    # Buy-Trigger: composite unter Regime-adjustierter Schwelle
    # Moderate erlaubt zusaetzlich triggered_dims > 0 wenn composite ausreichend niedrig
    triggered_ok = triggered == 0 if not is_moderate else triggered <= 2

    try:
        regime_mult = regime_buy_multiplier()
    except Exception:
        regime_mult = 1.0  # bei Fehler kein adjustierung
    effective_buy_max = config.score_buy_max * regime_mult

    if risk < effective_buy_max and triggered_ok:
        # Strategy-Label-Auswahl: ring 1 + sehr niedriges composite → long_term
        long_term_max = getattr(config, "long_term_composite_max", 25)
        if ring == 1 and risk < long_term_max:
            strategy_label = "long_term"
        else:
            strategy_label = "mid_term"

        return TradeDecision(
            ticker=ticker, action="buy",
            reason=(f"composite {risk:.1f} < {effective_buy_max:.1f} "
                    f"(strategy={strategy_label}, regime_mult={regime_mult:.2f}) "
                    f"alert={alert} triggered={triggered}"),
            target_eur=config.max_position_eur,
            risk_composite=risk, alert_level=alert,
            confidence=confidence,
            strategy_label=strategy_label,
            based_on_pred_id=score["pred_id"],
        )

    return TradeDecision(
        ticker=ticker, action="skip",
        reason=f"composite {risk:.1f} above effective buy-threshold {effective_buy_max:.1f} or triggered={triggered}",
        risk_composite=risk, alert_level=alert,
        confidence=confidence,
        based_on_pred_id=score["pred_id"],
    )


def log_decision(decision: TradeDecision, strategy_label: str = "conservative-v1") -> int:
    """
    Schreibt eine prediction-Row mit job_source='trade_decision'.
    Diese Row wird spaeter durch outcome_tracker mit T+1d/7d/30d-PnL annotiert.
    """
    pred_id = log_prediction(
        job_source="trade_decision",
        model=f"{strategy_label}-{decision.strategy_label}",
        subject_type="ticker",
        subject_id=decision.ticker,
        prompt=f"trading.decision / strategy={strategy_label} / horizon={decision.strategy_label}",
        input_payload={
            "ticker":          decision.ticker,
            "based_on":        decision.based_on_pred_id,
            "risk_composite":  decision.risk_composite,
            "alert_level":     decision.alert_level,
            "strategy_label":  decision.strategy_label,
        },
        input_summary=f"{decision.ticker} composite={decision.risk_composite:.1f} alert={decision.alert_level} {decision.strategy_label}",
        output={
            "action":         decision.action,
            "reason":         decision.reason,
            "target_eur":     decision.target_eur,
            "strategy_label": decision.strategy_label,
        },
        confidence=decision.confidence,
        cost_estimate_eur=0.0,
    )
    decision.decision_pred_id = pred_id
    return pred_id

"""
Position-Sizing.

Skaliert das target_eur einer Trade-Decision basierend auf:
  - Konfidenz   (high=100% / medium=60% / low=30%)
  - aktuelles Cash
  - min_position_eur Cap
  - ETF-Fallback wenn Sizing zu klein
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import TradingConfig
from .decision import TradeDecision


CONF_FACTOR = {"high": 1.00, "medium": 0.60, "low": 0.30}

# Vol-Targeting: ziel-Annual-Vola der Position. Bei niedrigerer Vola groessere
# Position, bei hoeherer kleinere. Cap bei 1.0 (nie ueber max_position_eur).
TARGET_VOL_ANNUAL = 0.25   # ~25% (NVDA-aehnliche Vola gilt als baseline)
MIN_SCALING       = 0.30   # bei extrem-Vola nicht unter 30% des Max gehen
MAX_SCALING       = 1.00


def asset_volatility_from_pred(ticker: str) -> float | None:
    """
    Holt annualisierte Volatility aus der juengsten daily_score-prediction.
    Returns None wenn nicht messbar.
    """
    from ..common.json_utils import safe_parse
    from ..common.storage import LEARNING_DB, connect

    sql = """
        SELECT output_json FROM predictions
         WHERE job_source='daily_score' AND subject_id=?
         ORDER BY created_at DESC LIMIT 1
    """
    try:
        with connect(LEARNING_DB) as conn:
            row = conn.execute(sql, (ticker,)).fetchone()
        if not row:
            return None
        out = safe_parse(row["output_json"] or "{}", default={})
        for d in out.get("dimensions", []):
            if d.get("name") == "volatility_30d":
                evidence = d.get("evidence", {})
                # Volatility-Dimension speichert Werte unterschiedlich — pruefen
                vol = evidence.get("volatility") or evidence.get("annualized_vol")
                if vol:
                    return float(vol)
                # Fallback: aus close_return_30d / volume_trend ableiten geht nicht.
                # Nutze die volatility_30d-Score selber als proxy (0-100 → 0-50% vola)
                score = d.get("score", 0)
                return min(0.6, max(0.10, score / 100 * 0.5 + 0.15))
    except Exception:
        return None
    return None


def vol_scaling(asset_vol: float | None) -> float:
    """
    Berechnet Position-Size-Scaling-Faktor basierend auf Asset-Vola.
    asset_vol=None → 1.0 (no adjustment, fallback).
    asset_vol=0.25 → 1.0 (Target-Match).
    asset_vol=0.50 → 0.50 (halbe Position).
    asset_vol=0.15 → 1.0 (gecapped, kein extra-leverage).
    """
    if asset_vol is None or asset_vol <= 0:
        return 1.0
    raw = TARGET_VOL_ANNUAL / asset_vol
    return max(MIN_SCALING, min(MAX_SCALING, raw))


@dataclass
class SizingResult:
    eur_amount:   float
    qty:          float
    skip:         bool
    skip_reason:  str = ""


def size_position(
    decision:     TradeDecision,
    cash_eur:     float,
    quote_usd:    float,
    fx_eur_per_usd: float,
    config:       TradingConfig,
) -> SizingResult:
    """
    Final eur-Volumen + Anzahl Aktien. Bei zu kleinen Resultaten skip.
    """
    if decision.action != "buy":
        return SizingResult(0.0, 0.0, skip=True, skip_reason="not a buy")

    factor = CONF_FACTOR.get(decision.confidence, 0.30)

    # Volatility-Targeting: pro-Asset-Adjustierung
    asset_vol = asset_volatility_from_pred(decision.ticker)
    vol_factor = vol_scaling(asset_vol)
    decision.extras["asset_vol_annual"] = asset_vol
    decision.extras["vol_scaling"]      = vol_factor

    target = min(decision.target_eur * factor * vol_factor, config.max_position_eur)
    target = min(target, cash_eur * 0.95)  # nicht 100% aufbrauchen

    if target < config.min_position_eur:
        return SizingResult(
            eur_amount=0.0, qty=0.0, skip=True,
            skip_reason=f"sized {target:.2f} EUR < min {config.min_position_eur:.2f}",
        )

    price_eur = quote_usd * fx_eur_per_usd
    if price_eur <= 0:
        return SizingResult(
            eur_amount=0.0, qty=0.0, skip=True,
            skip_reason="invalid price",
        )

    qty_raw = target / price_eur
    # Alpaca paper unterstuetzt fractional shares — round auf 4 Stellen
    qty = round(qty_raw, 4)
    eur = qty * price_eur

    if qty <= 0:
        return SizingResult(0.0, 0.0, skip=True, skip_reason="qty 0")

    return SizingResult(eur_amount=eur, qty=qty, skip=False)

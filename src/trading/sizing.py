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
    target = min(decision.target_eur * factor, config.max_position_eur)
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

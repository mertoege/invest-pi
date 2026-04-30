"""
Trading-Layer.

Domain-Logik fuer autonomes Paper-Trading auf dem Pi.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class TradingConfig:
    enabled:                    bool
    broker:                     str
    live_trading:               bool
    mode:                       str
    max_open_positions:         int
    max_position_eur:           float
    min_position_eur:           float
    starting_paper_capital:     float
    cash_floor_pct:             float
    max_per_sector_pct:         float
    score_buy_max:              int
    force_skip_alert_min:       int
    moderate_alert_max:         int
    stop_loss_pct:              float
    take_profit_pct:            float
    trailing_stop_pct:          float
    trailing_activation_pct:    float
    max_daily_loss_pct:         float
    max_trades_per_day:         int
    market_open_cet:            str
    market_close_cet:           str
    tradeable_rings:            list
    dca_fallback_ticker:        str
    sector_map:                 dict
    strategies:                 dict
    long_term_composite_max:    int
    regime_profiles:            dict


def load_trading_config(config_path: Optional[Path] = None) -> TradingConfig:
    """Liest settings.trading aus config.yaml. Defaults sind conservative."""
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    raw = yaml.safe_load(config_path.read_text())
    t = (raw or {}).get("settings", {}).get("trading", {})
    return TradingConfig(
        enabled=bool(t.get("enabled", False)),
        broker=t.get("broker", "mock"),
        live_trading=bool(t.get("live_trading", False)),
        mode=t.get("mode", "conservative"),
        max_open_positions=int(t.get("max_open_positions", 5)),
        max_position_eur=float(t.get("max_position_eur", 200)),
        min_position_eur=float(t.get("min_position_eur", 25)),
        starting_paper_capital=float(t.get("starting_paper_capital", 10000)),
        cash_floor_pct=float(t.get("cash_floor_pct", 0.20)),
        max_per_sector_pct=float(t.get("max_per_sector_pct", 0.40)),
        score_buy_max=int(t.get("score_buy_max", 25)),
        force_skip_alert_min=int(t.get("force_skip_alert_min", 2)),
        moderate_alert_max=int(t.get("moderate_alert_max", 1)),
        stop_loss_pct=float(t.get("stop_loss_pct", 0.15)),
        take_profit_pct=float(t.get("take_profit_pct", 0.20)),
        trailing_stop_pct=float(t.get("trailing_stop_pct", 0.08)),
        trailing_activation_pct=float(t.get("trailing_activation_pct", 0.12)),
        max_daily_loss_pct=float(t.get("max_daily_loss_pct", 0.05)),
        max_trades_per_day=int(t.get("max_trades_per_day", 3)),
        market_open_cet=str(t.get("market_open_cet", "15:30")),
        market_close_cet=str(t.get("market_close_cet", "22:00")),
        tradeable_rings=list(t.get("tradeable_rings", [1, 2])),
        dca_fallback_ticker=str(t.get("dca_fallback_ticker", "SMH")),
        sector_map=dict(t.get("sector_map", {})),
        strategies=dict(t.get("strategies", {})),
        long_term_composite_max=int(t.get("long_term_composite_max", 25)),
        regime_profiles=dict(t.get("regime_profiles", {})),
    )


def get_active_profile(config: TradingConfig) -> dict:
    """
    Returns die aktiven Schwellen basierend auf config.mode:
    - 'adaptive': nutzt aktuelles HMM-Regime und liest aus config.regime_profiles
    - sonst: returnt config-eigene Werte als Profile-Dict
    """
    if config.mode == "adaptive" and config.regime_profiles:
        try:
            from ..learning.regime import current_regime
            regime = current_regime()
            label = regime.label
        except Exception:
            label = "unknown"
        profile = config.regime_profiles.get(label) or config.regime_profiles.get("unknown") or {}
        if profile:
            return profile

    # Fallback: config-eigene Werte
    return {
        "score_buy_max":        config.score_buy_max,
        "max_open_positions":   config.max_open_positions,
        "max_position_eur":     config.max_position_eur,
        "max_trades_per_day":   config.max_trades_per_day,
        "stop_loss_pct":        config.stop_loss_pct,
        "take_profit_pct":      config.take_profit_pct,
        "trailing_activation":  config.trailing_activation_pct,
        "trailing_stop_pct":    config.trailing_stop_pct,
    }

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
    score_buy_max:              int
    force_skip_alert_min:       int
    stop_loss_pct:              float
    max_daily_loss_pct:         float
    max_trades_per_day:         int
    market_open_cet:            str
    market_close_cet:           str
    tradeable_rings:            list
    dca_fallback_ticker:        str


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
        score_buy_max=int(t.get("score_buy_max", 25)),
        force_skip_alert_min=int(t.get("force_skip_alert_min", 2)),
        stop_loss_pct=float(t.get("stop_loss_pct", 0.15)),
        max_daily_loss_pct=float(t.get("max_daily_loss_pct", 0.05)),
        max_trades_per_day=int(t.get("max_trades_per_day", 3)),
        market_open_cet=str(t.get("market_open_cet", "15:30")),
        market_close_cet=str(t.get("market_close_cet", "22:00")),
        tradeable_rings=list(t.get("tradeable_rings", [1, 2])),
        dca_fallback_ticker=str(t.get("dca_fallback_ticker", "SMH")),
    )

"""
Tests fuer mode-aware decision logic (conservative vs moderate).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-decision"
import shutil
shutil.rmtree(_TEST_DATA, ignore_errors=True)
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _setup():
    from src.common.storage import init_all
    init_all()
    from src.common.predictions import log_prediction
    # Verschiedene Predictions
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="MID",
        prompt="x",
        output={"composite": 35.0, "alert_level": 1, "triggered_n": 1},
        confidence="medium",
    )
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="LOW",
        prompt="x",
        output={"composite": 12.0, "alert_level": 0, "triggered_n": 0},
        confidence="low",
    )


def test_moderate_buys_mid_risk_ticker():
    _setup()
    from src.trading.decision import decide_action, TradingConfig
    cfg = TradingConfig(
        enabled=True, broker="mock", live_trading=False, mode="moderate",
        max_open_positions=8, max_position_eur=300, min_position_eur=25,
        starting_paper_capital=10000,
        cash_floor_pct=0.20, max_per_sector_pct=0.40,
        score_buy_max=45, force_skip_alert_min=2, moderate_alert_max=1,
        stop_loss_pct=0.10, take_profit_pct=0.20,
        trailing_stop_pct=0.08, trailing_activation_pct=0.12,
        max_daily_loss_pct=0.05, max_trades_per_day=5,
        market_open_cet="15:30", market_close_cet="22:00",
        tradeable_rings=[1, 2], dca_fallback_ticker="SMH",
        sector_map={},
    )
    # MID hat composite=35, alert=1 → moderate erlaubt
    d = decide_action("MID", set(), 0, ring=1, config=cfg)
    assert d.action == "buy", f"moderate sollte kaufen, hat aber {d.action} ({d.reason})"


def test_conservative_skips_mid_risk_ticker():
    _setup()
    from src.trading.decision import decide_action, TradingConfig
    cfg = TradingConfig(
        enabled=True, broker="mock", live_trading=False, mode="conservative",
        max_open_positions=5, max_position_eur=200, min_position_eur=25,
        starting_paper_capital=10000,
        cash_floor_pct=0.20, max_per_sector_pct=0.40,
        score_buy_max=25, force_skip_alert_min=2, moderate_alert_max=1,
        stop_loss_pct=0.15, take_profit_pct=0.20,
        trailing_stop_pct=0.08, trailing_activation_pct=0.12,
        max_daily_loss_pct=0.05, max_trades_per_day=3,
        market_open_cet="15:30", market_close_cet="22:00",
        tradeable_rings=[1, 2], dca_fallback_ticker="SMH",
        sector_map={},
    )
    # MID hat composite=35, das ist ueber conservative-Schwelle 25 → skip
    d = decide_action("MID", set(), 0, ring=1, config=cfg)
    assert d.action == "skip"


def test_moderate_buys_low_confidence():
    _setup()
    from src.trading.decision import decide_action, TradingConfig
    cfg = TradingConfig(
        enabled=True, broker="mock", live_trading=False, mode="moderate",
        max_open_positions=8, max_position_eur=300, min_position_eur=25,
        starting_paper_capital=10000,
        cash_floor_pct=0.20, max_per_sector_pct=0.40,
        score_buy_max=45, force_skip_alert_min=2, moderate_alert_max=1,
        stop_loss_pct=0.10, take_profit_pct=0.20,
        trailing_stop_pct=0.08, trailing_activation_pct=0.12,
        max_daily_loss_pct=0.05, max_trades_per_day=5,
        market_open_cet="15:30", market_close_cet="22:00",
        tradeable_rings=[1, 2], dca_fallback_ticker="SMH",
        sector_map={},
    )
    # LOW hat conf=low + composite=12 → moderate kauft
    d = decide_action("LOW", set(), 0, ring=1, config=cfg)
    assert d.action == "buy"


def test_take_profit_detection():
    from src.broker import get_broker
    from src.risk.limits import positions_to_take_profit
    from src.trading.decision import TradingConfig

    cfg = TradingConfig(
        enabled=True, broker="mock", live_trading=False, mode="moderate",
        max_open_positions=8, max_position_eur=300, min_position_eur=25,
        starting_paper_capital=10000,
        cash_floor_pct=0.20, max_per_sector_pct=0.40,
        score_buy_max=45, force_skip_alert_min=2, moderate_alert_max=1,
        stop_loss_pct=0.10, take_profit_pct=0.20,
        trailing_stop_pct=0.08, trailing_activation_pct=0.12,
        max_daily_loss_pct=0.05, max_trades_per_day=5,
        market_open_cet="15:30", market_close_cet="22:00",
        tradeable_rings=[1, 2], dca_fallback_ticker="SMH",
        sector_map={},
    )
    b = get_broker("mock")
    # Synthetic position: avg 100, current 130 (+30%) → take-profit
    b._positions["WIN"] = {"qty": 1.0, "avg_price_usd": 100.0, "opened_at": "2026-04-01"}
    orig = b._fetch_price
    b._fetch_price = lambda t: 130.0 if t == "WIN" else orig(t)

    triggered = positions_to_take_profit(b, cfg)
    assert any(t[0] == "WIN" for t in triggered)


def main() -> int:
    failed = 0
    for name, fn in [
        ("moderate_buys_mid_risk",     test_moderate_buys_mid_risk_ticker),
        ("conservative_skips_mid",     test_conservative_skips_mid_risk_ticker),
        ("moderate_buys_low_conf",     test_moderate_buys_low_confidence),
        ("take_profit_detection",      test_take_profit_detection),
    ]:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{4-failed}/4 passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

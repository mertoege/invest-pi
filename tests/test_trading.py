"""
End-to-End-Smoke fuer Trading-Layer.

Deckt:
  - MockBroker round-trip (account, quote, buy, position-tracking)
  - decide_action: alle Conservative-Branches (buy / skip Hard-Filter / skip Risk-Filter)
  - size_position mit Konfidenz-Scaling
  - kill_switch blockiert pre_trade_check
  - stop-loss-Detection via positions_to_stop_loss

Lauf:
    python tests/test_trading.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-trading"
shutil.rmtree(_TEST_DATA, ignore_errors=True)
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _seed_predictions():
    """Typische Risk-Scores fuer den Decision-Test."""
    from src.common.predictions import log_prediction
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="LOW_RISK",
        prompt="t", output={"composite": 12.0, "alert_level": 0, "triggered_n": 0},
        confidence="medium",
    )
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="HIGH_RISK",
        prompt="t", output={"composite": 65.0, "alert_level": 2, "triggered_n": 4},
        confidence="medium",
    )
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="LOW_CONF",
        prompt="t", output={"composite": 12.0, "alert_level": 0, "triggered_n": 0},
        confidence="low",
    )


def test_mock_broker_roundtrip():
    from src.broker import get_broker
    b = get_broker("mock", starting_capital_eur=5000.0)
    acc = b.get_account()
    assert acc.cash_eur == 5000.0
    assert b.get_quote("ANY").last > 0
    res = b.place_order("FOO", "buy", 1.0)
    assert res.status == "filled"
    assert res.filled_qty == 1.0
    positions = b.get_positions()
    assert len(positions) == 1 and positions[0].ticker == "FOO"
    # sell back
    res2 = b.place_order("FOO", "sell", 1.0)
    assert res2.status == "filled"
    assert b.get_positions() == []


def test_decision_branches():
    from src.common.storage import init_all
    init_all()
    _seed_predictions()
    from src.trading import load_trading_config
    from src.trading.decision import decide_action
    cfg = load_trading_config()

    # Hard skip: ring nicht tradeable
    d = decide_action("LOW_RISK", set(), 0, ring=3, config=cfg)
    assert d.action == "skip" and "ring 3" in d.reason

    # Hard skip: schon gehalten
    d = decide_action("LOW_RISK", {"LOW_RISK"}, 1, ring=1, config=cfg)
    assert d.action == "skip" and "already in positions" in d.reason

    # Hard skip: max positions
    d = decide_action("LOW_RISK", set(), cfg.max_open_positions, ring=1, config=cfg)
    assert d.action == "skip" and "max_open_positions" in d.reason

    # Skip: alert_level >= 2
    d = decide_action("HIGH_RISK", set(), 0, ring=1, config=cfg)
    assert d.action == "skip" and "alert_level" in d.reason

    # Skip: confidence low
    d = decide_action("LOW_CONF", set(), 0, ring=1, config=cfg)
    assert d.action == "skip" and "confidence low" in d.reason

    # Buy: composite niedrig + medium-conf + ring 1
    d = decide_action("LOW_RISK", set(), 0, ring=1, config=cfg)
    assert d.action == "buy"
    assert d.target_eur == cfg.max_position_eur


def test_sizing_confidence_scaling():
    from src.trading import load_trading_config
    from src.trading.decision import TradeDecision
    from src.trading.sizing import size_position
    cfg = load_trading_config()

    base = TradeDecision(ticker="X", action="buy", target_eur=200.0, confidence="medium",
                         reason="t", risk_composite=10, alert_level=0)
    sr = size_position(base, cash_eur=5000, quote_usd=100.0, fx_eur_per_usd=0.92, config=cfg)
    # medium = 60% factor → 120 EUR
    assert not sr.skip
    assert 119 < sr.eur_amount < 121

    # high
    base.confidence = "high"
    sr = size_position(base, cash_eur=5000, quote_usd=100.0, fx_eur_per_usd=0.92, config=cfg)
    assert 199 < sr.eur_amount < 201

    # low confidence skipped at decision-level normally, aber sizing-level: 30% von 200 = 60, > min 25 → kein skip
    base.confidence = "low"
    sr = size_position(base, cash_eur=5000, quote_usd=100.0, fx_eur_per_usd=0.92, config=cfg)
    assert 59 < sr.eur_amount < 61

    # cash zu wenig
    sr = size_position(base, cash_eur=10, quote_usd=100.0, fx_eur_per_usd=0.92, config=cfg)
    assert sr.skip


def test_kill_switch_blocks_pretrade():
    from src.broker import get_broker
    from src.trading import load_trading_config
    from src.risk.limits import (
        pre_trade_check, kill_switch_active,
        activate_kill_switch, deactivate_kill_switch,
    )
    deactivate_kill_switch()
    assert not kill_switch_active()
    broker = get_broker("mock")
    cfg = load_trading_config()

    activate_kill_switch("test")
    res = pre_trade_check(broker, cfg)
    assert not res.allowed and res.code == "kill"
    deactivate_kill_switch()


def test_stop_loss_detection():
    from src.broker import get_broker
    from src.broker.base import BrokerPosition
    from src.trading import load_trading_config
    from src.risk.limits import positions_to_stop_loss

    cfg = load_trading_config()
    b = get_broker("mock", starting_capital_eur=5000.0)
    # Create a position with simulated -20% drawdown by overriding the position dict directly
    b._positions["DOWN"] = {"qty": 2.0, "avg_price_usd": 100.0, "opened_at": "2026-04-01T00:00:00"}
    # Override price-fetch to simulate price 80 (-20%)
    orig = b._fetch_price
    b._fetch_price = lambda t: 80.0 if t == "DOWN" else orig(t)

    triggered = positions_to_stop_loss(b, cfg)
    assert any(t[0] == "DOWN" for t in triggered)


def main() -> int:
    failed = 0
    tests = [
        ("mock_broker_roundtrip", test_mock_broker_roundtrip),
        ("decision_branches",     test_decision_branches),
        ("sizing_confidence",     test_sizing_confidence_scaling),
        ("kill_switch_blocks",    test_kill_switch_blocks_pretrade),
        ("stop_loss_detection",   test_stop_loss_detection),
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

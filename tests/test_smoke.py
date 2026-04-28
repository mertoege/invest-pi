"""
Smoke-Test fuer die Phase-0+1-Foundation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Tests in einen sauberen temp-DataDir umleiten, BEVOR storage.py geladen wird.
_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-data"
_TEST_DATA.mkdir(parents=True, exist_ok=True)
for _db in _TEST_DATA.glob("*.db*"):
    _db.unlink()
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_imports() -> None:
    from src.common import storage, json_utils, predictions, cost_caps  # noqa
    try:
        from src.common import retry, outcomes  # noqa
    except ImportError as e:
        print(f"  (info) optional import skipped: {e}")


def test_init_all_creates_dbs() -> None:
    from src.common.storage import init_all, MARKET_DB, PATTERNS_DB, ALERTS_DB, LEARNING_DB
    init_all()
    for p in (MARKET_DB, PATTERNS_DB, ALERTS_DB, LEARNING_DB):
        assert p.exists(), f"DB nicht erstellt: {p}"
    init_all()  # idempotent


def test_strip_codefence() -> None:
    from src.common.json_utils import strip_codefence, safe_parse, extract_json_block

    cases = [
        ('```json\n{"a": 1}\n```',  {"a": 1}),
        ('```\n{"b": 2}\n```',       {"b": 2}),
        ('{"c": 3}',                  {"c": 3}),
        ('   ```json\n{"d": 4}\n```   ', {"d": 4}),
    ]
    for inp, expected in cases:
        parsed = safe_parse(inp)
        assert parsed == expected, f"safe_parse mismatch: {inp!r} -> {parsed} != {expected}"

    assert safe_parse("not json", default={"x": 0}) == {"x": 0}
    assert safe_parse("") == {}

    prose = 'Analyse: {"verdict": "buy", "confidence": "high"}'
    extracted = extract_json_block(prose)
    assert extracted is not None and json.loads(extracted)["verdict"] == "buy"


def test_prediction_lifecycle() -> None:
    from src.common.storage import init_all
    from src.common.predictions import (
        log_prediction, record_outcome, hit_rate, get_prediction,
        log_feedback, feedback_summary, hash_short,
    )
    init_all()

    pred_id = log_prediction(
        job_source="daily_score",
        model="heuristic-v1",
        subject_type="ticker",
        subject_id="TEST",
        prompt="smoke-test prompt",
        input_payload={"ticker": "TEST", "n": 9},
        input_summary="TEST, 9 dims",
        output={"composite": 55.0, "alert_level": 2},
        confidence="medium",
        cost_estimate_eur=0.0,
    )
    assert pred_id > 0

    rec = get_prediction(pred_id)
    assert rec is not None
    assert rec.subject_id == "TEST"
    assert rec.prompt_hash == hash_short("smoke-test prompt")
    assert rec.outcome_correct is None

    record_outcome(
        pred_id,
        outcome={"realized_drawdown_7d": -0.07, "alert_level": 2},
        correct=1,
    )
    rec2 = get_prediction(pred_id)
    assert rec2.outcome_correct == 1
    assert rec2.outcome_json is not None

    fb_id = log_feedback(pred_id, "false_positive", reason_code="macro",
                         reason_text="VIX-Spike")
    assert fb_id > 0
    summary = feedback_summary(days=1)
    assert any(r["reason_code"] == "macro" for r in summary["by_type_reason"])

    rates = hit_rate("daily_score", days=1)
    assert rates["measured"] >= 1
    assert rates["correct"] >= 1


def test_batch_aggregate_marker() -> None:
    from src.common.predictions import log_prediction, mark_batch_aggregate, get_prediction

    pid = log_prediction(
        job_source="score_batch",
        model="heuristic-v1",
        subject_type="batch",
        subject_id=None,
        prompt="batch-call",
        cost_estimate_eur=0.001,
    )
    mark_batch_aggregate(pid, reason="smoke-test")
    rec = get_prediction(pid)
    assert rec.outcome_correct is None
    assert rec.outcome_json is not None
    assert "batch_aggregate" in rec.outcome_json


def test_cost_caps() -> None:
    from src.common.cost_caps import check_budget, log_cost, cost_awareness_block

    state_before = check_budget()
    assert state_before.daily_cap > 0
    assert state_before.monthly_cap > 0

    log_cost("anthropic", 0.001, job_source="smoke-test")
    state_after = check_budget()
    assert state_after.daily_used >= state_before.daily_used

    block = cost_awareness_block(state_after)
    if state_after.cost_aware:
        assert block is not None
        assert "KOSTEN-MODUS" in block


def main() -> int:
    failed = 0
    tests = [
        ("imports", test_imports),
        ("init_all_creates_dbs", test_init_all_creates_dbs),
        ("strip_codefence", test_strip_codefence),
        ("prediction_lifecycle", test_prediction_lifecycle),
        ("batch_aggregate_marker", test_batch_aggregate_marker),
        ("cost_caps", test_cost_caps),
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

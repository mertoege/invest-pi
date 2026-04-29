"""
Tests fuer src/learning/calibration.py + meta_reviews-Read-Path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-calibration"
import shutil
shutil.rmtree(_TEST_DATA, ignore_errors=True)
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_calibration_block_empty():
    from src.common.storage import init_all
    init_all()
    from src.learning.calibration import calibration_block
    # Ohne Daten: empty string
    assert calibration_block("daily_score") == ""


def test_calibration_block_with_predictions():
    from src.common.storage import init_all
    init_all()
    from src.common.predictions import log_prediction, record_outcome
    from src.learning.calibration import calibration_block

    # Eine prediction mit outcome
    pid = log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="TEST",
        prompt="x", output={"alert_level": 0}, confidence="high",
    )
    record_outcome(pid, outcome={"check": "ok"}, correct=1)

    block = calibration_block("daily_score")
    assert "Lern-Statistik" in block
    assert "1" in block   # 1 prediction


def test_calibration_block_with_meta_review():
    from src.common.storage import init_all, LEARNING_DB, connect
    init_all()
    from src.learning.calibration import calibration_block, latest_meta_review

    action_plan = {
        "prio_1": ["composite-Schwelle senken auf 35"],
        "prio_2": ["NVDA in Watch-Liste setzen"],
        "prio_3": []
    }
    with connect(LEARNING_DB) as conn:
        conn.execute(
            """
            INSERT INTO meta_reviews
                (period_start, period_end, job_source, summary_md, action_plan_js)
            VALUES ('2026-04-01', '2026-04-30', 'daily_score',
                    '# test summary', ?)
            """,
            (json.dumps(action_plan),)
        )

    review = latest_meta_review("daily_score")
    assert review is not None
    assert "composite-Schwelle senken" in str(review["action_plan"])

    block = calibration_block("daily_score")
    assert "Meta-Review" in block or "composite" in block


def main() -> int:
    failed = 0
    for name, fn in [
        ("empty",            test_calibration_block_empty),
        ("with_predictions", test_calibration_block_with_predictions),
        ("with_meta_review", test_calibration_block_with_meta_review),
    ]:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{3-failed}/3 passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

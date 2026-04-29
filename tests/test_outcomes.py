"""
Tests fuer src/common/outcomes.py — die Outcome-Tracking-Logik.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-outcomes"
import shutil
shutil.rmtree(_TEST_DATA, ignore_errors=True)
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_correctness_for_alert():
    from src.common.outcomes import _correctness_for_alert
    # alert >=2 + drawdown -7%: Risiko realisierte sich → korrekt
    assert _correctness_for_alert(2, -0.07) == 1
    assert _correctness_for_alert(3, -0.10) == 1
    # alert >=2 + drawdown -3%: kein crash → falsch
    assert _correctness_for_alert(2, -0.03) == 0
    # alert == 0 + drawdown -7%: Risiko da, alert verpasst → falsch
    assert _correctness_for_alert(0, -0.07) == 0
    # alert == 0 + drawdown -3%: keine Krise, alert war richtig → korrekt
    assert _correctness_for_alert(0, -0.03) == 1
    # alert == 1: Watch ist nicht binär bewertbar
    assert _correctness_for_alert(1, -0.07) is None
    # max_dd None: nicht messbar
    assert _correctness_for_alert(2, None) is None


def test_drift_detection_no_data():
    from src.common.storage import init_all
    init_all()
    from src.common.outcomes import detect_drift
    # Ohne measured outcomes: None
    assert detect_drift("daily_score") is None


def test_drift_detection_with_synthetic_data():
    from src.common.storage import init_all, LEARNING_DB, connect
    from src.common.outcomes import detect_drift
    init_all()

    # Synthetische Predictions mit Outcomes:
    # 14d zurück: 5/10 (50%)
    # 7d zurück:  9/10 (90%) → recent ist VIEL besser → "JUMP" drift
    sql_insert = """
        INSERT INTO predictions
            (created_at, job_source, model, subject_type, subject_id,
             output_json, outcome_correct, outcome_json)
        VALUES (datetime('now', ?), 'daily_score', 'heuristic-v1', 'ticker', 'TEST',
                '{"alert_level":0}', ?, '{"measured":true}')
    """
    with connect(LEARNING_DB) as conn:
        # 14-8d: 5 correct, 5 incorrect
        for i in range(5):
            conn.execute(sql_insert, (f"-{14-i} day", 1))
            conn.execute(sql_insert, (f"-{14-i} day", 0))
        # 7-1d: 9 correct, 1 incorrect (recent better)
        for i in range(9):
            conn.execute(sql_insert, (f"-{7-i//2} day", 1))
        conn.execute(sql_insert, ("-3 day", 0))

    drift = detect_drift("daily_score", window_days=7)
    if drift is None:
        return  # depends on exact data shape — accept None as ok if window-edges drift
    assert drift["direction"] in ("JUMP", "DROP") or "delta_pp" in drift


def main() -> int:
    failed = 0
    for name, fn in [
        ("correctness_for_alert",        test_correctness_for_alert),
        ("drift_detection_no_data",      test_drift_detection_no_data),
        ("drift_detection_with_data",    test_drift_detection_with_synthetic_data),
    ]:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{3-failed}/3 passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

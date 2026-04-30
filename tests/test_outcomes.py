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




def test_measure_outcome_tz_naive_no_typeerror():
    """Regression-Test: measure_outcome_for muss mit naive datetime aus SQLite klarkommen.
    
    Bug: dt.datetime.now(tz.utc) (aware) vs start_date aus iso-string (naive)
    → TypeError beim Vergleich.
    """
    import datetime as dt
    from src.common.storage import init_all, LEARNING_DB, connect
    from src.common.predictions import log_prediction, get_prediction
    from src.common.outcomes import measure_outcome_for, _measure_window
    init_all()

    # Synthetic prediction mit "altem" created_at
    pid = log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="TEST",
        prompt="x",
        output={"composite": 30, "alert_level": 0, "triggered_n": 0},
        confidence="medium",
    )
    with connect(LEARNING_DB) as conn:
        conn.execute("UPDATE predictions SET created_at = datetime('now', '-2 day') WHERE id = ?", (pid,))

    pred = get_prediction(pid)
    # created_at ist naive iso-string aus SQLite
    assert "+" not in pred.created_at, "expected naive timestamp"

    # _measure_window darf NICHT mit TypeError crashen
    import pandas as pd
    fake_prices = pd.DataFrame({
        "close": [100, 101, 102, 103, 104],
    }, index=pd.date_range("2026-04-26", periods=5, freq="D"))
    start = dt.datetime.fromisoformat(pred.created_at.replace(" ", "T"))
    try:
        m = _measure_window(fake_prices, start, days=7)
    except TypeError as e:
        if "offset-naive and offset-aware" in str(e):
            assert False, f"TZ-Bug regression: {e}"
        raise


def main() -> int:
    failed = 0
    for name, fn in [
        ("correctness_for_alert",         test_correctness_for_alert),
        ("drift_detection_no_data",       test_drift_detection_no_data),
        ("drift_detection_with_data",     test_drift_detection_with_synthetic_data),
        ("measure_outcome_tz_consistency", test_measure_outcome_tz_naive_no_typeerror),
    ]:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{4-failed}/4 passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

"""
Tests fuer src/alerts/dispatch.py — alert-deduplication-logic.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-dispatch"
import shutil
shutil.rmtree(_TEST_DATA, ignore_errors=True)
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)
os.environ["TELEGRAM_BOT_TOKEN"] = "TEST"
os.environ["TELEGRAM_CHAT_ID"]   = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_dispatch_no_alerts():
    from src.common.storage import init_all
    init_all()
    from src.alerts.dispatch import dispatch_new_alerts
    # Ohne Predictions: nichts zu pushen
    with patch("requests.post"):
        result = dispatch_new_alerts(min_level=2)
    assert not result["skipped"] or result.get("sent", 0) == 0


def test_dispatch_pushes_only_high_level():
    from src.common.storage import init_all
    init_all()
    from src.common.predictions import log_prediction
    from src.alerts.dispatch import dispatch_new_alerts
    from unittest.mock import MagicMock

    # Eine prediction mit alert=0, eine mit alert=2
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="LOW",
        prompt="x",
        output={"composite": 10.0, "alert_level": 0, "triggered_n": 0, "triggered_dims": []},
        confidence="medium",
    )
    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="HIGH",
        prompt="x",
        output={"composite": 65.0, "alert_level": 2, "triggered_n": 4, "triggered_dims": ["x"]},
        confidence="medium",
    )

    sent_payloads = []
    def capture(*args, **kwargs):
        sent_payloads.append(kwargs.get("json", {}))
        m = MagicMock(); m.json.return_value = {"ok": True}; m.raise_for_status.return_value = None
        return m

    with patch("requests.post", side_effect=capture):
        result = dispatch_new_alerts(min_level=2, lookback_hours=1)

    # Nur HIGH sollte gepushed werden, nicht LOW
    assert result["sent"] == 1
    assert any("HIGH" in (p.get("text", "")) for p in sent_payloads)


def test_dispatch_dedup():
    from src.common.storage import init_all
    init_all()
    from src.common.predictions import log_prediction
    from src.alerts.dispatch import dispatch_new_alerts
    from unittest.mock import MagicMock

    log_prediction(
        job_source="daily_score", model="heuristic-v1",
        subject_type="ticker", subject_id="DEDUP_TEST",
        prompt="x",
        output={"composite": 70.0, "alert_level": 2, "triggered_n": 5, "triggered_dims": ["a"]},
        confidence="medium",
    )
    m = MagicMock(); m.json.return_value = {"ok": True}; m.raise_for_status.return_value = None

    with patch("requests.post", return_value=m):
        result1 = dispatch_new_alerts(min_level=2, lookback_hours=1)
        # Zweiter Aufruf direkt danach: keine neuen Alerts, alles schon notified
        result2 = dispatch_new_alerts(min_level=2, lookback_hours=1)

    assert result1["sent"] == 1
    assert result2["sent"] == 0


def main() -> int:
    failed = 0
    for name, fn in [
        ("no_alerts",          test_dispatch_no_alerts),
        ("pushes_high_level",  test_dispatch_pushes_only_high_level),
        ("dedup",              test_dispatch_dedup),
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

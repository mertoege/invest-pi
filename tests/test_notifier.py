"""
Tests fuer src/alerts/notifier.py — Telegram-API mit Mock.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

_TEST_DATA = Path(tempfile.gettempdir()) / "invest-pi-test-notifier"
import shutil
shutil.rmtree(_TEST_DATA, ignore_errors=True)
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["INVEST_PI_DATA_DIR"] = str(_TEST_DATA)
os.environ["TELEGRAM_BOT_TOKEN"] = "TEST_TOKEN_123"
os.environ["TELEGRAM_CHAT_ID"] = "987654321"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_mock_response(ok=True):
    m = MagicMock()
    m.json.return_value = {"ok": ok, "result": {}}
    m.raise_for_status.return_value = None
    return m


def test_is_configured():
    from src.alerts.notifier import is_configured
    assert is_configured()


def test_send_alert_with_buttons():
    from src.common.storage import init_all
    init_all()
    from src.alerts.notifier import send_alert

    with patch("requests.post", return_value=_make_mock_response(True)) as mock_post:
        ok = send_alert(
            ticker="NVDA",
            alert_level=2,
            composite=58.3,
            triggered_dims=["technical_breakdown", "macro_regime"],
            prediction_id=42,
            dimensions_summary="<i>Test</i>",
        )
        assert ok is True
        # Verify Inline-Buttons im payload
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs.get("json") or call_kwargs.get("data") or {}
        assert "reply_markup" in payload
        assert "fb:42:" in payload["reply_markup"]


def test_send_alert_html_escape():
    from src.common.storage import init_all
    init_all()
    from src.alerts.notifier import send_alert

    captured_text = []
    def capture(*args, **kwargs):
        # Aus dem POST-payload den text rausnehmen
        captured_text.append(kwargs.get("json", {}).get("text", ""))
        return _make_mock_response(True)

    with patch("requests.post", side_effect=capture):
        send_alert(
            ticker="WEIRD<>", alert_level=1, composite=20.0,
            triggered_dims=["dim<x>"], prediction_id=1,
        )

    assert any("&lt;" in t or "&gt;" in t for t in captured_text)


def test_send_trade_html_escape():
    from src.common.storage import init_all
    init_all()
    from src.alerts.notifier import send_trade

    captured_text = []
    def capture(*args, **kwargs):
        captured_text.append(kwargs.get("json", {}).get("text", ""))
        return _make_mock_response(True)

    with patch("requests.post", side_effect=capture):
        ok = send_trade(
            ticker="NVDA", side="buy", qty=1.0,
            eur=100, price_usd=110,
            reason="composite < 25",
            paper=True,
        )
    assert ok is True
    # composite < 25 muss escaped sein als 'composite &lt; 25'
    assert any("&lt; 25" in t for t in captured_text)


def test_send_alert_telegram_400():
    from src.common.storage import init_all
    init_all()
    from src.alerts.notifier import send_alert
    import requests

    fail_resp = MagicMock()
    fail_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("400 Bad Request")
    with patch("requests.post", return_value=fail_resp):
        ok = send_alert(
            ticker="X", alert_level=2, composite=50.0,
            triggered_dims=[], prediction_id=99,
        )
    # Failures muessen False zurueckgeben, nicht crashen
    assert ok is False


def main() -> int:
    failed = 0
    for name, fn in [
        ("is_configured",          test_is_configured),
        ("send_alert_with_buttons", test_send_alert_with_buttons),
        ("send_alert_html_escape", test_send_alert_html_escape),
        ("send_trade_html_escape", test_send_trade_html_escape),
        ("send_alert_400",         test_send_alert_telegram_400),
    ]:
        try:
            fn()
            print(f"  OK {name}")
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{5-failed}/5 passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())

"""Test Per-Ticker-Reliability-Adjustment im risk_scorer (Audit 2026-06-18)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.alerts import risk_scorer
from src.common import predictions


def _patch_hit_rate(measured, hit_rate_val):
    def fake(job_source, days=30, confidence=None, subject_id=None, by_measured=False):
        return {"measured": measured, "hit_rate": hit_rate_val,
                "correct": 0, "incorrect": 0, "total": measured, "pending": 0}
    predictions.hit_rate = fake


def test_insufficient_samples_no_adjustment():
    _patch_hit_rate(measured=5, hit_rate_val=0.50)
    delta, info = risk_scorer._ticker_reliability_adjustment("XYZ")
    assert delta == 0.0
    assert info.get("reliability_applied") is False


def test_low_hitrate_malus():
    # PLTR-artig: 62% bei 100 gemessen -> Malus (Composite rauf)
    _patch_hit_rate(measured=100, hit_rate_val=0.62)
    delta, info = risk_scorer._ticker_reliability_adjustment("PLTR")
    assert delta > 0
    assert delta <= risk_scorer.RELIABILITY_MAX_MALUS
    assert info["reliability_applied"] is True


def test_very_low_hitrate_capped():
    _patch_hit_rate(measured=100, hit_rate_val=0.40)
    delta, _ = risk_scorer._ticker_reliability_adjustment("BAD")
    assert delta == risk_scorer.RELIABILITY_MAX_MALUS  # gedeckelt


def test_high_hitrate_bonus():
    _patch_hit_rate(measured=100, hit_rate_val=0.97)
    delta, info = risk_scorer._ticker_reliability_adjustment("JNJ")
    assert delta < 0
    assert delta >= -risk_scorer.RELIABILITY_MAX_BONUS
    assert info["reliability_applied"] is True


def test_neutral_band_no_adjustment():
    # 80% liegt zwischen LOW(70) und HIGH(90) -> keine Anpassung
    _patch_hit_rate(measured=100, hit_rate_val=0.80)
    delta, _ = risk_scorer._ticker_reliability_adjustment("MID")
    assert delta == 0.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nalle reliability-tests bestanden")

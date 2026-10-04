"""Crash-Bremse der Momentum-Engine: unter dem 200-Tage-Schnitt nur halb investiert."""
import datetime as dt
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import scripts.momentum_rebalance as mr


def _spy(values):
    idx = pd.date_range(end=dt.date.today() - dt.timedelta(days=1), periods=len(values), freq="D")
    return pd.DataFrame({"close": values}, index=idx)


def test_ueber_trend_voll(monkeypatch):
    monkeypatch.setattr(mr, "get_prices", lambda *a, **k: _spy([100 + i * 0.1 for i in range(300)]))
    assert mr._market_exposure() == 1.0


def test_unter_trend_halb(monkeypatch):
    monkeypatch.setattr(mr, "get_prices", lambda *a, **k: _spy([200 - i * 0.2 for i in range(300)]))
    assert mr._market_exposure() == mr.TREND_EXPO


def test_ohne_daten_vormonat(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(mr, "get_prices", boom)
    assert mr._market_exposure(0.5) == 0.5
    assert mr._market_exposure(None) == 1.0


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    orig = mr.get_prices
    for t in (test_ueber_trend_voll, test_unter_trend_halb, test_ohne_daten_vormonat):
        t(_MP()); mr.get_prices = orig
        print(f"OK  {t.__name__}")

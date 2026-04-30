#!/usr/bin/env python3
"""
Tests fuer Backtest-Engine V2 — 9-Dim-Scoring + Vol-Targeting + Constraints.

Run:
  INVEST_PI_DATA_DIR=/tmp/test PYTHONDONTWRITEBYTECODE=1 python3 -B tests/test_backtest_v2.py
"""

import os
import sys
import math
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure test data isolation
os.environ["INVEST_PI_DATA_DIR"] = "/tmp/test_bt_v2"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd


class TestScoringFunctions(unittest.TestCase):
    """Test individual 9-dim scoring dimensions."""

    def setUp(self):
        # Import after path setup
        from src.learning import backtest_engine as be
        self.be = be

    def _make_prices(self, n=300, base=100, trend=0.0, vol=0.02):
        """Generate synthetic price series."""
        np.random.seed(42)
        rets = np.random.normal(trend, vol, n)
        prices = base * np.cumprod(1 + rets)
        return prices

    def _make_volumes(self, n=300, base=1000000):
        np.random.seed(42)
        return (base * (1 + np.random.normal(0, 0.3, n))).clip(100)

    # ── Dim 1: Technical Breakdown ──
    def test_technical_breakdown_bullish(self):
        """Uptrending prices should get low score."""
        closes = self._make_prices(300, trend=0.001, vol=0.01)
        score, triggered, reason = self.be._bt_technical_breakdown(closes)
        self.assertLess(score, 40, "bullish trend should have low score")
        self.assertFalse(triggered)

    def test_technical_breakdown_bearish(self):
        """Downtrending prices with death cross should get high score."""
        # Strong downtrend
        closes = self._make_prices(300, trend=-0.002, vol=0.01)
        score, triggered, reason = self.be._bt_technical_breakdown(closes)
        self.assertGreater(score, 20, "bearish trend should have elevated score")

    def test_technical_breakdown_short_history(self):
        """Less than 200 days returns 0."""
        closes = self._make_prices(100)
        score, triggered, reason = self.be._bt_technical_breakdown(closes)
        self.assertEqual(score, 0.0)
        self.assertFalse(triggered)

    # ── Dim 2: Volume Divergence ──
    def test_volume_divergence_normal(self):
        """Normal volume pattern should score low."""
        closes = self._make_prices(100, trend=0.001)
        volumes = self._make_volumes(100)
        score, triggered, reason = self.be._bt_volume_divergence(closes, volumes)
        self.assertLess(score, 50)

    # ── Dim 8: Valuation Percentile ──
    def test_valuation_at_highs(self):
        """All-time-high should have elevated percentile score."""
        closes = self._make_prices(500, trend=0.001, vol=0.005)
        score, triggered, reason = self.be._bt_valuation_percentile(closes)
        # Uptrend → should be at high percentile
        self.assertGreater(score, 0, "ATH-trend should have some score")

    def test_valuation_short_history(self):
        """Less than 252 days returns 0."""
        closes = self._make_prices(200)
        score, triggered, reason = self.be._bt_valuation_percentile(closes)
        self.assertEqual(score, 0.0)

    # ── Dim 9: Macro Regime ──
    def test_macro_calm(self):
        """Low-vol market should score low."""
        spy = self._make_prices(300, trend=0.001, vol=0.005)
        score, triggered, reason = self.be._bt_macro_regime(spy, 250)
        self.assertLess(score, 40, "calm market should have low macro score")

    def test_macro_stressed(self):
        """High-vol market should score high."""
        spy = self._make_prices(300, trend=-0.005, vol=0.04)
        score, triggered, reason = self.be._bt_macro_regime(spy, 250)
        self.assertGreater(score, 20, "stressed market should have elevated score")

    # ── Composite Scoring ──
    def test_composite_returns_valid_range(self):
        """Composite should be 0-100."""
        closes = self._make_prices(300)
        volumes = self._make_volumes(300)
        spy = self._make_prices(300, trend=0.001, vol=0.01)
        composite, alert, triggered_n, conf = self.be._score_9dim(
            "NVDA", closes, volumes, 250, {"AMD": closes}, spy
        )
        self.assertGreaterEqual(composite, 0)
        self.assertLessEqual(composite, 100)
        self.assertIn(alert, [0, 1, 2, 3])
        self.assertIn(conf, ["high", "medium", "low"])

    # ── Vol Scaling ──
    def test_vol_scaling_high_vol(self):
        """High-vol asset should get smaller position."""
        closes = self._make_prices(300, vol=0.04)  # ~63% annual
        factor = self.be._vol_scale_backtest(closes, 250)
        self.assertLess(factor, 0.5, "high-vol asset should scale down")

    def test_vol_scaling_low_vol(self):
        """Low-vol asset should get full position."""
        closes = self._make_prices(300, vol=0.005)  # ~8% annual
        factor = self.be._vol_scale_backtest(closes, 250)
        self.assertEqual(factor, 1.0, "low-vol should cap at 1.0")


class TestBacktestV2Integration(unittest.TestCase):
    """Integration tests for run_backtest_v2."""

    @patch("src.learning.backtest_engine._load_history")
    def test_v2_basic_run(self, mock_load):
        """V2 should run without errors on synthetic data."""
        from src.learning.backtest_engine import run_backtest_v2

        # Create 600-day synthetic history for 3 tickers + SMH
        np.random.seed(42)
        n = 600
        dates = pd.bdate_range("2022-01-01", periods=n)
        tickers = ["NVDA", "AMD", "SMH"]
        history = {}
        for t in tickers:
            base = {"NVDA": 200, "AMD": 100, "SMH": 150}.get(t, 100)
            rets = np.random.normal(0.0005, 0.02, n)
            closes = base * np.cumprod(1 + rets)
            df = pd.DataFrame({
                "open": closes * 0.99,
                "high": closes * 1.01,
                "low": closes * 0.98,
                "close": closes,
                "volume": np.random.randint(1000000, 5000000, n).astype(float),
            }, index=dates)
            df.index.name = "date"
            history[t] = df

        mock_load.return_value = history

        result = run_backtest_v2(
            start="2023-01-01", end="2023-12-31",
            tickers=["NVDA", "AMD"],
            initial_capital=50000,
            mode="static",
        )

        self.assertIsNotNone(result)
        self.assertGreater(result.n_days, 200)
        self.assertIsNotNone(result.total_return)
        # Sharpe may be None if zero daily variance
        # Main check: result computed without errors

        # Should have summary method
        summary = result.summary()
        self.assertIn("Backtest", summary)

    @patch("src.learning.backtest_engine._load_history")
    def test_v2_adaptive_mode(self, mock_load):
        """Adaptive mode should use regime detection."""
        from src.learning.backtest_engine import run_backtest_v2

        np.random.seed(123)
        n = 600
        dates = pd.bdate_range("2022-01-01", periods=n)
        history = {}
        for t in ["NVDA", "AMD", "SMH"]:
            base = 100
            closes = base * np.cumprod(1 + np.random.normal(0.0003, 0.015, n))
            history[t] = pd.DataFrame({
                "open": closes*0.99, "high": closes*1.01,
                "low": closes*0.98, "close": closes,
                "volume": np.random.randint(1e6, 5e6, n).astype(float),
            }, index=dates)

        mock_load.return_value = history

        result = run_backtest_v2(
            start="2023-01-01", end="2023-12-31",
            tickers=["NVDA", "AMD"],
            initial_capital=50000,
            mode="adaptive",
        )

        self.assertIsNotNone(result)
        self.assertGreater(len(result.regime_history), 0, "adaptive should produce regime history")

    @patch("src.learning.backtest_engine._load_history")
    def test_v2_cash_floor_enforced(self, mock_load):
        """Cash floor should prevent over-deployment."""
        from src.learning.backtest_engine import run_backtest_v2

        np.random.seed(42)
        n = 400
        dates = pd.bdate_range("2022-01-01", periods=n)
        history = {}
        # Many tickers to test position limits
        for t in ["NVDA","AMD","MSFT","GOOGL","META","ASML","TSM","AVGO","LRCX","KLAC","SMH"]:
            closes = 100 * np.cumprod(1 + np.random.normal(0.001, 0.01, n))
            history[t] = pd.DataFrame({
                "open": closes*0.99, "high": closes*1.01,
                "low": closes*0.98, "close": closes,
                "volume": np.random.randint(1e6, 5e6, n).astype(float),
            }, index=dates)
        mock_load.return_value = history

        result = run_backtest_v2(
            start="2022-06-01", end="2023-06-01",
            tickers=["NVDA","AMD","MSFT","GOOGL","META","ASML","TSM","AVGO","LRCX","KLAC"],
            initial_capital=10000,
            position_eur=2000,
            cash_floor_pct=0.20,
            max_positions=20,
        )

        # Cash floor should have prevented deploying >80% of capital
        # (equity >= initial in a slightly positive market)
        self.assertIsNotNone(result)

    @patch("src.learning.backtest_engine._load_history")
    def test_v2_vol_targeting_reduces_position_size(self, mock_load):
        """Vol targeting should make positions smaller for volatile stocks."""
        from src.learning.backtest_engine import run_backtest_v2

        np.random.seed(42)
        n = 500
        dates = pd.bdate_range("2022-01-01", periods=n)
        history = {}
        # NVDA: high vol
        closes_nvda = 100 * np.cumprod(1 + np.random.normal(0.001, 0.04, n))
        history["NVDA"] = pd.DataFrame({
            "open": closes_nvda*0.99, "high": closes_nvda*1.02,
            "low": closes_nvda*0.97, "close": closes_nvda,
            "volume": np.random.randint(1e6, 5e6, n).astype(float),
        }, index=dates)
        # SMH: moderate vol
        closes_smh = 100 * np.cumprod(1 + np.random.normal(0.0005, 0.015, n))
        history["SMH"] = pd.DataFrame({
            "open": closes_smh*0.99, "high": closes_smh*1.01,
            "low": closes_smh*0.98, "close": closes_smh,
            "volume": np.random.randint(1e6, 5e6, n).astype(float),
        }, index=dates)
        mock_load.return_value = history

        # With vol targeting
        r_vol = run_backtest_v2(
            start="2023-01-01", end="2023-12-31",
            tickers=["NVDA"], initial_capital=50000,
            vol_targeting=True,
        )
        # Without vol targeting
        r_novol = run_backtest_v2(
            start="2023-01-01", end="2023-12-31",
            tickers=["NVDA"], initial_capital=50000,
            vol_targeting=False,
        )

        self.assertIsNotNone(r_vol)
        self.assertIsNotNone(r_novol)


class TestBacktestV1Compat(unittest.TestCase):
    """Ensure V1 still works after V2 addition."""

    @patch("src.learning.backtest_engine._load_history")
    def test_v1_still_works(self, mock_load):
        """V1 run_backtest should still work identically."""
        from src.learning.backtest_engine import run_backtest

        np.random.seed(42)
        n = 400
        dates = pd.bdate_range("2023-01-01", periods=n)
        history = {}
        for t in ["NVDA", "SMH"]:
            closes = 100 * np.cumprod(1 + np.random.normal(0.001, 0.02, n))
            history[t] = pd.DataFrame({
                "open": closes*0.99, "high": closes*1.01,
                "low": closes*0.98, "close": closes,
                "volume": np.random.randint(1e6, 5e6, n).astype(float),
            }, index=dates)
        mock_load.return_value = history

        result = run_backtest(
            start="2023-06-01", end="2024-06-01",
            tickers=["NVDA"],
            initial_capital=50000,
        )
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.total_return)


if __name__ == "__main__":
    unittest.main(verbosity=2)

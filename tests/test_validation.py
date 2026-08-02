"""
Tests for src/strategy/validation.py

These tests use synthetic data. The point is to verify the methodology
works, not to validate the cascade strategy on real data (that happens
in scripts/run_backtest.py).
"""

import numpy as np
import pandas as pd
import pytest

from src.strategy.validation import (
    parameter_sweep,
    walk_forward,
    StabilityReport,
    WalkForwardResult,
    SweepResult,
    WalkForwardFold,
)


def make_synth_candles(n: int = 200, base: float = 100.0, start: str = "2026-01-01"):
    """Build flat candles for ATR/timing."""
    times = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": times,
        "open": [base] * n,
        "high": [base] * n,
        "low": [base] * n,
        "close": [base] * n,
        "volume": [100.0] * n,
    })


def make_synth_funding(rates, start: str = "2026-01-01", coin: str = "BTC"):
    times = pd.date_range(start, periods=len(rates), freq="8h", tz="UTC")
    return pd.DataFrame({
        "timestamp": times,
        "coin": [coin] * len(rates),
        "funding_rate": rates,
        "premium": [r * 0.5 for r in rates],
    })


class TestParameterSweep:
    def test_basic_sweep_returns_report(self):
        candles = {"BTC": make_synth_candles()}
        # Mostly flat funding with a couple of extreme events
        rates = [0.0001] * 50 + [0.0015, 0.0012, 0.0018, 0.0009, 0.0011] + [0.0001] * 45
        funding = {"BTC": make_synth_funding(rates)}
        report = parameter_sweep(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            high_thresholds=[0.0010, 0.0015, 0.0020],
            low_thresholds=[-0.0005, -0.0010],
        )
        assert isinstance(report, StabilityReport)
        assert len(report.results) > 0
        assert all(isinstance(r, SweepResult) for r in report.results)

    def test_sweep_dataframe_round_trip(self):
        candles = {"BTC": make_synth_candles()}
        rates = [0.0001] * 50 + [0.0015] * 5 + [0.0001] * 45
        funding = {"BTC": make_synth_funding(rates)}
        report = parameter_sweep(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            high_thresholds=[0.0010, 0.0015],
            low_thresholds=[-0.0005],
        )
        df = report.to_dataframe()
        assert "high_threshold" in df.columns
        assert "return_pct" in df.columns
        assert "win_rate" in df.columns
        assert len(df) == len(report.results)

    def test_stability_assessment_strict_threshold(self):
        candles = {"BTC": make_synth_candles()}
        rates = [0.0001] * 50 + [0.0015] * 5 + [0.0001] * 45
        funding = {"BTC": make_synth_funding(rates)}
        report = parameter_sweep(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            high_thresholds=[0.0010, 0.0015, 0.0020],
            low_thresholds=[-0.0005],
        )
        # is_stable is a bool
        assert isinstance(report.is_stable, bool)
        # CV should be a non-negative number
        assert report.pnl_coefficient_of_variation >= 0
        # pct_configs_profitable in 0-100
        assert 0 <= report.pct_configs_profitable <= 100

    def test_invalid_threshold_combo_skipped(self):
        """If high < |low|/2, the combo is skipped as nonsensical."""
        candles = {"BTC": make_synth_candles()}
        rates = [0.0001] * 50 + [0.0015] * 5 + [0.0001] * 45
        funding = {"BTC": make_synth_funding(rates)}
        report = parameter_sweep(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            high_thresholds=[0.0001],  # very tight
            low_thresholds=[-0.0050],  # very wide — should be skipped
        )
        # The combo high_t < abs(low_t)*0.5 = 0.0025, so 0.0001 < 0.0025, skip
        # Should produce 0 results
        assert len(report.results) == 0


class TestWalkForward:
    def test_basic_walk_forward(self):
        # 3 folds, 60% train / 40% test each
        n_candles = 2000  # ~83 days of hourly data
        candles = {"BTC": make_synth_candles(n=n_candles)}
        # Mix of periods with and without funding extremes
        rates = [0.0001] * 200 + [0.0015] * 10 + [0.0001] * 200 + [0.0015] * 10 + [0.0001] * 200 + [0.0015] * 10
        rates = (rates * (n_candles // len(rates) + 1))[:(n_candles // 8)]
        funding = {"BTC": make_synth_funding(rates)}

        result = walk_forward(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            n_folds=3,
            train_frac=0.6,
            high_threshold=0.0010,
            low_threshold=-0.0005,
        )
        assert isinstance(result, WalkForwardResult)
        assert result.n_folds == 3
        assert len(result.folds) == 3
        for fold in result.folds:
            assert isinstance(fold, WalkForwardFold)

    def test_fold_dataframe_round_trip(self):
        n_candles = 500
        candles = {"BTC": make_synth_candles(n=n_candles)}
        rates = [0.0001] * 50 + [0.0015] * 5 + [0.0001] * 45
        rates = (rates * (n_candles // len(rates) + 1))[:(n_candles // 8)]
        funding = {"BTC": make_synth_funding(rates)}

        result = walk_forward(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            n_folds=3,
            train_frac=0.6,
        )
        df = result.to_dataframe()
        assert "fold_id" in df.columns
        assert "train_pnl_usd" in df.columns
        assert "test_pnl_usd" in df.columns
        assert len(df) == 3

    def test_consistent_flag_present(self):
        n_candles = 500
        candles = {"BTC": make_synth_candles(n=n_candles)}
        rates = [0.0001] * 50 + [0.0015] * 5 + [0.0001] * 45
        rates = (rates * (n_candles // len(rates) + 1))[:(n_candles // 8)]
        funding = {"BTC": make_synth_funding(rates)}

        result = walk_forward(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            n_folds=3,
        )
        assert isinstance(result.consistent_across_folds, bool)
        assert 0 <= result.pct_folds_profitable <= 100

    def test_train_test_split_correctness(self):
        """The first fold's train + test should cover the same period with no overlap."""
        n_candles = 800
        candles = {"BTC": make_synth_candles(n=n_candles)}
        rates = [0.0001] * 100
        funding = {"BTC": make_synth_funding(rates)}

        result = walk_forward(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            n_folds=3,
            train_frac=0.6,
        )
        # First fold: train_start < train_end == test_start < test_end
        first = result.folds[0]
        assert first.train_start < first.train_end
        assert first.train_end == first.test_start
        assert first.test_start < first.test_end

    def test_degradation_metric_present(self):
        n_candles = 500
        candles = {"BTC": make_synth_candles(n=n_candles)}
        rates = [0.0001] * 50 + [0.0015] * 5 + [0.0001] * 45
        rates = (rates * (n_candles // len(rates) + 1))[:(n_candles // 8)]
        funding = {"BTC": make_synth_funding(rates)}

        result = walk_forward(
            candles_by_symbol=candles,
            funding_by_symbol=funding,
            n_folds=3,
        )
        # Degradation is a number (can be negative if test is better than train)
        assert isinstance(result.test_pnl_degradation_pct, (int, float))

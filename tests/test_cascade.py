"""
Tests for src/strategy/cascade.py — pure unit tests, no I/O.
"""

import pandas as pd
import pytest

from src.strategy.cascade import (
    CascadeSignal,
    SignalDirection,
    detect_funding_extreme,
    summarize_funding_extremes,
)


def make_funding_df(rates, coin="BTC"):
    """Build a synthetic funding history DataFrame."""
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-08-01", periods=len(rates), freq="8h", tz="UTC"
            ),
            "coin": coin,
            "funding_rate": rates,
            "premium": [r * 0.5 for r in rates],
        }
    )


class TestDetectFundingExtreme:
    def test_empty_df_returns_empty(self):
        df = pd.DataFrame(columns=["timestamp", "coin", "funding_rate", "premium"])
        assert detect_funding_extreme(df) == []

    def test_normal_rates_no_signals(self):
        df = make_funding_df([0.0001, 0.0002, -0.0001, 0.00005])
        assert detect_funding_extreme(df) == []

    def test_high_funding_emits_short(self):
        df = make_funding_df([0.0015])  # 0.15% per 8h
        signals = detect_funding_extreme(df)
        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SHORT
        assert signals[0].confidence > 0.3

    def test_low_funding_emits_long(self):
        df = make_funding_df([-0.0008])  # -0.08% per 8h
        signals = detect_funding_extreme(df)
        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.LONG
        assert signals[0].confidence > 0.3

    def test_confidence_grows_with_extreme(self):
        df_mild = make_funding_df([0.0011])
        df_wild = make_funding_df([0.0050])
        mild_signal = detect_funding_extreme(df_mild)[0]
        wild_signal = detect_funding_extreme(df_wild)[0]
        assert wild_signal.confidence > mild_signal.confidence

    def test_confidence_capped_at_one(self):
        df = make_funding_df([0.05])
        signal = detect_funding_extreme(df)[0]
        assert signal.confidence <= 1.0

    def test_confidence_floor_enforced(self):
        # Just barely above threshold = should still emit at floor confidence
        df = make_funding_df([0.001001])  # 0.1001%
        signals = detect_funding_extreme(df)
        assert len(signals) == 1
        # Floor is 0.3, slope is 5, excess is ~0.000001, so conf ~= 0.3 + 0.0005 ~= 0.3005
        assert signals[0].confidence >= 0.3

    def test_custom_thresholds(self):
        df = make_funding_df([0.0008])  # 0.08% — under default 0.10%
        # Default threshold: no signal
        assert detect_funding_extreme(df) == []
        # Custom lower threshold: signal fires
        signals = detect_funding_extreme(df, high_threshold=0.0005)
        assert len(signals) == 1

    def test_reason_includes_values(self):
        df = make_funding_df([0.0015])
        signal = detect_funding_extreme(df)[0]
        assert "0.1500%" in signal.reason
        assert "HIGH" in signal.reason

    def test_low_reason_includes_LOW(self):
        df = make_funding_df([-0.0010])
        signal = detect_funding_extreme(df)[0]
        assert "LOW" in signal.reason
        assert "-0.1000%" in signal.reason

    def test_mixed_extremes(self):
        rates = [0.0001, 0.0015, -0.0008, 0.0002, 0.0020, -0.0006, 0.00005]
        df = make_funding_df(rates)
        signals = detect_funding_extreme(df)
        assert len(signals) == 4
        shorts = [s for s in signals if s.direction == SignalDirection.SHORT]
        longs = [s for s in signals if s.direction == SignalDirection.LONG]
        assert len(shorts) == 2
        assert len(longs) == 2


class TestSummarizeFundingExtremes:
    def test_empty(self):
        df = pd.DataFrame(columns=["timestamp", "coin", "funding_rate", "premium"])
        summary = summarize_funding_extremes(df)
        assert summary["count_high"] == 0
        assert summary["count_low"] == 0
        assert summary["total_periods"] == 0

    def test_mixed(self):
        rates = [0.0001, 0.0015, -0.0008, 0.0002, 0.0020, -0.0006]
        df = make_funding_df(rates)
        summary = summarize_funding_extremes(df)
        assert summary["count_high"] == 2
        assert summary["count_low"] == 2
        assert summary["max_high"] == pytest.approx(0.0020)
        assert summary["min_low"] == pytest.approx(-0.0008)
        assert summary["total_periods"] == 6

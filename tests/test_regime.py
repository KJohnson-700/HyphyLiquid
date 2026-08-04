"""Tests for deterministic regime labels and routing."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.regime import (  # noqa: E402
    band_width_bucket,
    classify_candle_regime,
    classify_liquidation_response,
    route_signal,
)


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _bar(t_ms: int, c: float, h: float | None = None, l: float | None = None) -> dict:
    return {
        "t": t_ms,
        "o": c,
        "h": h if h is not None else c + 0.05,
        "l": l if l is not None else c - 0.05,
        "c": c,
    }


class TestRegimeLabels(unittest.TestCase):
    def test_band_width_bucket_boundaries(self):
        self.assertEqual(band_width_bucket(0.5), "compressed")
        self.assertEqual(band_width_bucket(0.75), "normal")
        self.assertEqual(band_width_bucket(1.5), "wide")
        self.assertEqual(band_width_bucket(2.5), "very_wide")

    def test_classifies_range_bucket_from_prior_candles(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        closes = [99.9, 100.0, 100.1, 100.0] * 5
        candles = [_bar(base + i * 60000, c=close) for i, close in enumerate(closes)]
        candles.append(_bar(base + 20 * 60000, c=110.0))

        regime = classify_candle_regime(candles, 20)

        self.assertEqual(regime.trend, "range")
        self.assertEqual(regime.label, "range_compressed")
        self.assertLessEqual(regime.band_width_pct, 0.5)

    def test_classifies_trend_before_current_bar(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [_bar(base + i * 60000, c=100 + i * 0.2) for i in range(21)]

        regime = classify_candle_regime(
            candles,
            20,
            trend_threshold_pct=0.2,
            high_atr_pct=10.0,
        )

        self.assertEqual(regime.label, "trend_up")
        self.assertGreaterEqual(regime.slope_pct, 0.2)

    def test_high_atr_overrides_range_label(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base + i * 60000, c=100.0, h=102.0, l=98.0)
            for i in range(21)
        ]

        regime = classify_candle_regime(candles, 20, high_atr_pct=1.0)

        self.assertEqual(regime.label, "high_vol_cascade")
        self.assertIsNotNone(regime.atr_pct)


class TestLiquidationResponse(unittest.TestCase):
    def test_b_side_reclaim_closes_back_below_vwap(self):
        response = classify_liquidation_response("B", 100.0, [101.0, 99.9, 99.5])

        self.assertEqual(response.label, "post_liquidation_reclaim")
        self.assertTrue(response.reclaim_detected)
        self.assertEqual(response.bars_checked, 2)

    def test_a_side_failed_reclaim_continuation(self):
        response = classify_liquidation_response("A", 100.0, [99.0, 99.2, 99.5])

        self.assertEqual(response.label, "post_liquidation_continuation")
        self.assertFalse(response.reclaim_detected)


class TestRegimeRouting(unittest.TestCase):
    def test_btc_b_side_continuation_is_v1_watch(self):
        candle = classify_candle_regime(
            [_bar(_ms("2026-08-03T00:00:00+00:00") + i * 60000, c=100 + i * 0.2) for i in range(21)],
            20,
            high_atr_pct=10.0,
        )
        response = classify_liquidation_response("B", 100.0, [101.0, 101.2, 101.5])

        route = route_signal("BTC", "B", candle, response)

        self.assertEqual(route.action, "watch")
        self.assertTrue(route.execution_allowed)
        self.assertEqual(route.lane, "btc_eth_trailing_resolution")

    def test_hype_b_side_normal_range_is_research_only(self):
        candle = classify_candle_regime(
            [_bar(_ms("2026-08-03T00:00:00+00:00") + i * 60000, c=close)
             for i, close in enumerate([99.7, 100.0, 100.3, 100.0] * 5)],
            20,
            trend_threshold_pct=1.0,
            high_atr_pct=10.0,
        )
        response = classify_liquidation_response("B", 100.0, [100.2, 99.8])

        route = route_signal("HYPE", "B", candle, response)

        self.assertEqual(route.action, "research_candidate")
        self.assertFalse(route.execution_allowed)
        self.assertEqual(route.lane, "alt_range_liq_scalp")

    def test_hype_compressed_range_rejects(self):
        candle = classify_candle_regime(
            [_bar(_ms("2026-08-03T00:00:00+00:00") + i * 60000, c=100.0) for i in range(21)],
            20,
            high_atr_pct=10.0,
        )
        response = classify_liquidation_response("B", 100.0, [100.2, 99.8])

        route = route_signal("HYPE", "B", candle, response)

        self.assertEqual(route.action, "reject")
        self.assertFalse(route.execution_allowed)

    def test_eth_rejects_until_new_framing_passes(self):
        candle = classify_candle_regime(
            [_bar(_ms("2026-08-03T00:00:00+00:00") + i * 60000, c=100 + i * 0.1) for i in range(21)],
            20,
            high_atr_pct=10.0,
        )
        response = classify_liquidation_response("B", 100.0, [101.0, 101.2])

        route = route_signal("ETH", "B", candle, response)

        self.assertEqual(route.action, "reject")
        self.assertTrue(route.execution_allowed)


if __name__ == "__main__":
    unittest.main()

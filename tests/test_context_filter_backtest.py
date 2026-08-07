"""Tests for the Tier-1 context filter backtest."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_context_filter_backtest import (  # noqa: E402
    BucketVerdict,
    _funding_z_score,
    _pct_delta,
    apply_promotion_gate,
    compute_context_features,
    cooldown_bucket,
    funding_z_bucket,
    oi_price_regime,
    summarize_bucket,
)


class TestContextFeatureMath(unittest.TestCase):
    def test_funding_z_score_uses_prior_history_only(self):
        rows = [{"ts": i * 60_000, "funding": 1.0, "oi": 100, "mark": 100} for i in range(30)]
        rows.append({"ts": 31 * 60_000, "funding": 3.0, "oi": 100, "mark": 100})

        z = _funding_z_score(rows, 30, lookback=30, min_history=30)

        self.assertEqual(z, 0.0)  # zero stdev history returns neutral sentinel

    def test_funding_z_bucket_boundaries(self):
        self.assertEqual(funding_z_bucket(None), "funding_unknown")
        self.assertEqual(funding_z_bucket(2.1), "funding_pos_extreme")
        self.assertEqual(funding_z_bucket(-2.1), "funding_neg_extreme")
        self.assertEqual(funding_z_bucket(1.1), "funding_pos_elevated")
        self.assertEqual(funding_z_bucket(-1.1), "funding_neg_elevated")
        self.assertEqual(funding_z_bucket(0.2), "funding_normal")

    def test_pct_delta(self):
        self.assertAlmostEqual(_pct_delta(110, 100), 10.0)
        self.assertAlmostEqual(_pct_delta(90, 100), -10.0)
        self.assertIsNone(_pct_delta(100, 0))

    def test_oi_price_regime(self):
        self.assertEqual(oi_price_regime(0.10, 0.20), "price_up_oi_up")
        self.assertEqual(oi_price_regime(-0.10, -0.20), "price_down_oi_down")
        self.assertEqual(oi_price_regime(0.10, -0.20), "price_up_oi_down")
        self.assertEqual(oi_price_regime(-0.10, 0.20), "price_down_oi_up")
        self.assertEqual(oi_price_regime(0.01, 0.01), "price_flat_oi_flat")
        self.assertEqual(oi_price_regime(None, 0.1), "oi_price_unknown")

    def test_cooldown_bucket(self):
        self.assertEqual(cooldown_bucket(None), "first_cascade")
        self.assertEqual(cooldown_bucket(3), "cooldown_hot_lt5m")
        self.assertEqual(cooldown_bucket(10), "cooldown_warm_5_15m")
        self.assertEqual(cooldown_bucket(30), "cooldown_room_15_60m")
        self.assertEqual(cooldown_bucket(90), "cooldown_fresh_60m_plus")

    def test_compute_context_features_combines_all_three(self):
        ctx = []
        for i in range(40):
            ctx.append({"ts": i * 60_000, "funding": 0.001, "oi": 100 + i * 0.1, "mark": 100 + i * 0.1})
        cascade = {
            "symbol": "BTC",
            "side": "B",
            "event_ts_ms": 39 * 60_000,
            "start_ts": "2026-08-04T00:39:00+00:00",
        }

        features = compute_context_features(cascade, ctx, prior_ts_ms=30 * 60_000)

        self.assertEqual(features.symbol, "BTC")
        self.assertEqual(features.funding_z_bucket, "funding_normal")
        self.assertEqual(features.oi_price_regime, "price_up_oi_up")
        self.assertEqual(features.cooldown_bucket, "cooldown_warm_5_15m")


class TestContextBucketStats(unittest.TestCase):
    def test_summarize_bucket_profit_factor_and_gate(self):
        values = [0.30, 0.20, 0.10, -0.05] * 8

        verdict = summarize_bucket("BTC", "B", 15, "fade", "funding_z:funding_pos_extreme", values)

        self.assertEqual(verdict.n, 32)
        self.assertGreater(verdict.pf, 1.5)
        self.assertGreater(verdict.median_pnl_pct, 0)
        self.assertTrue(verdict.passed)

    def test_promotion_gate_rejects_small_negative_or_concentrated_buckets(self):
        small = BucketVerdict("BTC", "B", 15, "fade", "x", 3, 100, 0.1, 0.1, 99, 0.1)
        self.assertFalse(apply_promotion_gate(small).passed)
        negative = BucketVerdict("BTC", "B", 15, "fade", "x", 30, 50, 0.1, -0.01, 2.0, 0.1)
        self.assertFalse(apply_promotion_gate(negative).passed)
        concentrated = BucketVerdict("BTC", "B", 15, "fade", "x", 30, 60, 0.1, 0.1, 2.0, 0.5)
        self.assertFalse(apply_promotion_gate(concentrated).passed)


if __name__ == "__main__":
    unittest.main()

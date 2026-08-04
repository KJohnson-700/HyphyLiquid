from __future__ import annotations

import unittest

from src.strategy.filter_diagnostics import (
    diagnostic_groups,
    enrich_trades_with_filters,
    funding_bucket,
    imbalance_bucket,
    notional_bucket,
    spread_bucket,
    staleness_bucket,
)


class TestFilterBuckets(unittest.TestCase):
    def test_spread_bucket(self) -> None:
        self.assertEqual(spread_bucket(None), "missing")
        self.assertEqual(spread_bucket(0.1), "tight")
        self.assertEqual(spread_bucket(0.6), "normal")
        self.assertEqual(spread_bucket(2.0), "wide")

    def test_imbalance_bucket(self) -> None:
        self.assertEqual(imbalance_bucket(None), "missing")
        self.assertEqual(imbalance_bucket(-0.5), "ask_heavy")
        self.assertEqual(imbalance_bucket(0.0), "balanced")
        self.assertEqual(imbalance_bucket(0.5), "bid_heavy")

    def test_funding_bucket(self) -> None:
        self.assertEqual(funding_bucket(None), "missing")
        self.assertEqual(funding_bucket(-0.00001), "negative")
        self.assertEqual(funding_bucket(0.0), "flat")
        self.assertEqual(funding_bucket(0.00001), "positive")

    def test_size_and_staleness_buckets(self) -> None:
        self.assertEqual(notional_bucket(250_000), "lt_500k")
        self.assertEqual(notional_bucket(750_000), "500k_1m")
        self.assertEqual(notional_bucket(2_000_000), "1m_3m")
        self.assertEqual(notional_bucket(4_000_000), "gte_3m")
        self.assertEqual(staleness_bucket(-4.0), "fresh")
        self.assertEqual(staleness_bucket(25.0), "usable")
        self.assertEqual(staleness_bucket(90.0), "stale")
        self.assertEqual(staleness_bucket(200.0), "too_stale")


class TestFilterDiagnostics(unittest.TestCase):
    def test_enrich_trades_with_filters_joins_on_cascade_key(self) -> None:
        cascades = [
            {
                "start_ts": "2026-08-04T00:00:00+00:00",
                "symbol": "BTC",
                "side": "B",
                "bbo_spread_bps": 0.2,
                "top_book_imbalance": 0.7,
                "funding": "0.00001",
                "predicted_funding": "-0.00001",
                "total_notional": 2_000_000,
                "n_fills": 150,
                "l2_delta_s": 2.0,
                "ctx_delta_s": 20.0,
                "oi": "100",
            }
        ]
        trades = [
            {
                "cascade_start_ts": "2026-08-04T00:00:00+00:00",
                "symbol": "BTC",
                "side": "B",
                "variant": "baseline_fade",
                "return_pct": 0.1,
            }
        ]
        rows = enrich_trades_with_filters(trades, cascades)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bbo_spread_bucket"], "tight")
        self.assertEqual(rows[0]["top_book_imbalance_bucket"], "bid_heavy")
        self.assertEqual(rows[0]["funding_bucket"], "positive")
        self.assertEqual(rows[0]["predicted_funding_bucket"], "negative")
        self.assertEqual(rows[0]["notional_bucket"], "1m_3m")
        self.assertEqual(rows[0]["fill_count_bucket"], "gte_100")
        self.assertEqual(rows[0]["l2_staleness_bucket"], "fresh")
        self.assertEqual(rows[0]["ctx_staleness_bucket"], "usable")

    def test_diagnostic_groups_applies_min_n(self) -> None:
        rows = [
            {
                "symbol": "BTC",
                "side": "B",
                "variant": "baseline_fade",
                "bbo_spread_bucket": "tight",
                "top_book_imbalance_bucket": "balanced",
                "funding_bucket": "flat",
                "predicted_funding_bucket": "flat",
                "notional_bucket": "1m_3m",
                "fill_count_bucket": "25_99",
                "l2_staleness_bucket": "fresh",
                "ctx_staleness_bucket": "fresh",
                "oi_level_bucket": "mid",
                "return_pct": 0.1 if i % 2 == 0 else -0.05,
            }
            for i in range(6)
        ]
        out = diagnostic_groups(rows, min_n=5)
        self.assertTrue(any(row["bucket"] == "symbol=BTC" for row in out))
        self.assertFalse(any(row["n"] < 5 for row in out))


if __name__ == "__main__":
    unittest.main()

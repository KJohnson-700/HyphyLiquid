"""Tests for src/strategy/event_features.py."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.event_features import (
    _bbo_from_l2book,
    _asset_ctx_features,
    snapshot_event_features,
    write_event_features,
    EVENT_FEATURES_PATH,
)


class TestBboFromL2Book(unittest.TestCase):
    def test_none_payload(self):
        out = _bbo_from_l2book(None)
        self.assertEqual(out["bbo_spread"], None)
        self.assertEqual(out["top_book_imbalance"], None)

    def test_empty_levels(self):
        self.assertEqual(_bbo_from_l2book({"levels": [[], []]})["bbo_spread"], None)
        self.assertEqual(_bbo_from_l2book({"levels": []})["bbo_spread"], None)

    def test_basic_spread(self):
        payload = {
            "coin": "BTC",
            "levels": [
                [{"px": "100.0", "sz": "1.0"}, {"px": "99.5", "sz": "2.0"}],
                [{"px": "100.5", "sz": "1.0"}, {"px": "101.0", "sz": "2.0"}],
            ],
        }
        out = _bbo_from_l2book(payload)
        self.assertAlmostEqual(out["bbo_spread"], 0.5)
        # mid = 100.25, spread / mid * 10000 = 0.5 / 100.25 * 10000 ~= 49.875 bps
        self.assertAlmostEqual(out["bbo_spread_bps"], 49.875, places=2)
        # top-3 bid sz = 1.0 + 2.0 = 3.0, top-3 ask sz = 1.0 + 2.0 = 3.0, imbalance = 0
        self.assertAlmostEqual(out["top_book_imbalance"], 0.0)

    def test_imbalance_long_heavy(self):
        payload = {
            "levels": [
                [{"px": "100.0", "sz": "5.0"}],
                [{"px": "100.5", "sz": "1.0"}],
            ],
        }
        out = _bbo_from_l2book(payload)
        # bid 5, ask 1, total 6, imbalance = (5-1)/6 = 0.6667
        self.assertAlmostEqual(out["top_book_imbalance"], 4 / 6, places=4)


class TestAssetCtxFeatures(unittest.TestCase):
    def test_none(self):
        out = _asset_ctx_features(None)
        self.assertEqual(out["oi"], None)

    def test_basic(self):
        record = {
            "poll_ts": "2026-08-02T10:00:00+00:00",
            "symbol": "BTC",
            "context": {
                "markPx": "63000.0",
                "oraclePx": "63001.0",
                "funding": "0.0000125",
                "openInterest": "12345.6",
            },
            "predicted": {
                "HlPerp": {"fundingRate": "0.0000125"},
            },
        }
        out = _asset_ctx_features(record)
        self.assertEqual(out["oi"], "12345.6")
        self.assertEqual(out["funding"], "0.0000125")
        self.assertEqual(out["predicted_funding"], "0.0000125")
        self.assertEqual(out["mark_px"], "63000.0")


class TestSnapshotEventFeatures(unittest.TestCase):
    def test_event_with_no_l2_or_ctx(self):
        # No l2book / asset_ctx files present -> null features, but event
        # primitives should still come through. Use a far-future date so
        # the test never collides with real daemon output.
        event = {
            "ts": "2099-01-01T10:00:00+00:00",
            "symbol": "ZZZ_FUTURE_TEST",
            "side": "A",
            "total_notional": 1000000.0,
            "n_fills": 50,
            "price_avg": 63000.0,
            "duration_ms": 800,
            "confidence": 0.7,
            "reason": "test",
        }
        out = snapshot_event_features(event)
        self.assertEqual(out["symbol"], "ZZZ_FUTURE_TEST")
        self.assertEqual(out["side"], "A")
        self.assertEqual(out["event_vwap"], 63000.0)
        self.assertEqual(out["total_notional"], 1000000.0)
        self.assertEqual(out["bbo_spread"], None)
        self.assertEqual(out["oi"], None)
        self.assertEqual(out["post_5m_return"], None)


if __name__ == "__main__":
    unittest.main()

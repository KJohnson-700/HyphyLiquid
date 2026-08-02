"""Tests for src/strategy/cascade_cluster.py."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.cascade_cluster import cluster_events


def _ev(ts, symbol, side, notional=1_000_000, n_fills=50, price_avg=63000.0, confidence=0.7):
    return {
        "ts": ts,
        "symbol": symbol,
        "side": side,
        "total_notional": notional,
        "n_fills": n_fills,
        "price_avg": price_avg,
        "confidence": confidence,
    }


class TestClusterEmpty(unittest.TestCase):
    def test_no_events(self):
        self.assertEqual(cluster_events([]), [])


class TestClusterSingle(unittest.TestCase):
    def test_single_event_becomes_one_cluster(self):
        events = [_ev("2026-08-02T10:00:00+00:00", "BTC", "A")]
        out = cluster_events(events)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["symbol"], "BTC")
        self.assertEqual(c["side"], "A")
        self.assertEqual(c["start_ts"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(c["end_ts"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(c["n_events"], 1)
        self.assertEqual(c["total_notional"], 1_000_000)
        self.assertEqual(c["n_fills"], 50)
        self.assertEqual(c["event_vwap"], 63000.0)
        self.assertEqual(c["max_confidence"], 0.7)


class TestClusterBurst(unittest.TestCase):
    def test_tight_burst_merges(self):
        # 5 events within 2s, all same side -> 1 cluster
        events = [
            _ev(f"2026-08-02T10:00:0{i}+00:00", "BTC", "A", price_avg=63000.0 + i, confidence=0.6 + i * 0.05)
            for i in range(5)
        ]
        out = cluster_events(events)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["n_events"], 5)
        self.assertEqual(c["total_notional"], 5_000_000)
        self.assertEqual(c["n_fills"], 250)
        # VWAP: each event has same notional (1M), so VWAP = avg(price_avg)
        # prices are 63000, 63001, 63002, 63003, 63004 -> avg = 63002
        self.assertAlmostEqual(c["event_vwap"], 63002.0, places=4)
        # max confidence is 0.6 + 4*0.05 = 0.8
        self.assertAlmostEqual(c["max_confidence"], 0.8)
        self.assertEqual(c["start_ts"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(c["end_ts"], "2026-08-02T10:00:04+00:00")


class TestClusterSplitByDirection(unittest.TestCase):
    def test_same_symbol_different_sides_stay_separate(self):
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "A", notional=1_000_000),
            _ev("2026-08-02T10:00:01+00:00", "BTC", "B", notional=2_000_000),
        ]
        out = cluster_events(events)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["side"], "A")
        self.assertEqual(out[0]["n_events"], 1)
        self.assertEqual(out[1]["side"], "B")
        self.assertEqual(out[1]["n_events"], 1)


class TestClusterSplitBySymbol(unittest.TestCase):
    def test_different_symbols_stay_separate(self):
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "A"),
            _ev("2026-08-02T10:00:01+00:00", "ETH", "A"),
        ]
        out = cluster_events(events)
        self.assertEqual(len(out), 2)
        self.assertEqual({c["symbol"] for c in out}, {"BTC", "ETH"})


class TestClusterWindowBoundary(unittest.TestCase):
    def test_default_60s_window(self):
        # 30s gap -> still merged (within 60s window)
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "A", notional=1_000_000),
            _ev("2026-08-02T10:00:30+00:00", "BTC", "A", notional=1_000_000),
        ]
        out = cluster_events(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["n_events"], 2)

    def test_120s_gap_splits_at_60s_window(self):
        # 120s gap -> split at 60s default
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "A", notional=1_000_000),
            _ev("2026-08-02T10:02:00+00:00", "BTC", "A", notional=1_000_000),
        ]
        out = cluster_events(events, time_window_s=60)
        self.assertEqual(len(out), 2)

    def test_120s_gap_merges_at_180s_window(self):
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "A", notional=1_000_000),
            _ev("2026-08-02T10:02:00+00:00", "BTC", "A", notional=1_000_000),
        ]
        out = cluster_events(events, time_window_s=180)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["n_events"], 2)


class TestClusterRealExampleFromData(unittest.TestCase):
    def test_three_close_events_become_one(self):
        # Mirrors the actual data: 3 BTC events within 50 seconds
        events = [
            _ev("2026-08-02T10:30:15.533+00:00", "BTC", "B", notional=1_000_000, price_avg=63000.0, n_fills=50),
            _ev("2026-08-02T10:30:15.534+00:00", "BTC", "B", notional=1_000_000, price_avg=63001.0, n_fills=50),
            _ev("2026-08-02T10:30:15.535+00:00", "BTC", "B", notional=1_000_000, price_avg=63002.0, n_fills=50),
        ]
        out = cluster_events(events)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["n_events"], 3)
        self.assertEqual(c["total_notional"], 3_000_000)
        self.assertEqual(c["n_fills"], 150)
        self.assertAlmostEqual(c["event_vwap"], 63001.0, places=4)


if __name__ == "__main__":
    unittest.main()

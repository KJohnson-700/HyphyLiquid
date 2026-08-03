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

    def test_interleaved_symbols_do_not_break_same_symbol_cluster(self):
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "A", notional=1_000_000),
            _ev("2026-08-02T10:00:01+00:00", "ETH", "A", notional=1_000_000),
            _ev("2026-08-02T10:00:02+00:00", "BTC", "A", notional=1_000_000),
        ]
        out = cluster_events(events, time_window_s=60)
        btc = [c for c in out if c["symbol"] == "BTC"]
        eth = [c for c in out if c["symbol"] == "ETH"]
        self.assertEqual(len(btc), 1)
        self.assertEqual(btc[0]["n_events"], 2)
        self.assertEqual(len(eth), 1)


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


class TestClusterVWAPMath(unittest.TestCase):
    """The OLD (wrong) formula: event_vwap = sum(price_avg * notional) / sum(notional)
    This is biased toward the higher-price sub-event.

    The CORRECT formula: event_vwap = total_notional / total_size
    where total_size = sum_i(notional_i / price_avg_i).
    This is the size-weighted average across all fills, which is the
    standard VWAP definition.
    """

    def test_vwap_equal_notional_equal_price(self):
        # Sanity: equal notional, equal price -> VWAP = price
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B", notional=1_000_000, price_avg=100.0),
            _ev("2026-08-02T10:00:01+00:00", "BTC", "B", notional=1_000_000, price_avg=100.0),
        ]
        out = cluster_events(events)
        self.assertAlmostEqual(out[0]["event_vwap"], 100.0, places=4)

    def test_vwap_unequal_notional_same_price(self):
        # $2M at $100, $1M at $100 -> VWAP = $100 (size-weighted)
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B", notional=2_000_000, price_avg=100.0),
            _ev("2026-08-02T10:00:01+00:00", "BTC", "B", notional=1_000_000, price_avg=100.0),
        ]
        out = cluster_events(events)
        self.assertAlmostEqual(out[0]["event_vwap"], 100.0, places=4)

    def test_vwap_unequal_notional_different_price(self):
        """The key test. Sub-event A: 1M @ $100 = 10k units.
        Sub-event B: 1M @ $50 = 20k units. Total = 30k units, $2M.
        True VWAP = $2M / 30k = $66.67.
        OLD (wrong) formula would give: (100*1M + 50*1M) / 2M = $75.
        """
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B", notional=1_000_000, price_avg=100.0),
            _ev("2026-08-02T10:00:01+00:00", "BTC", "B", notional=1_000_000, price_avg=50.0),
        ]
        out = cluster_events(events)
        vwap = out[0]["event_vwap"]
        # True VWAP = 66.67 (size-weighted)
        self.assertAlmostEqual(vwap, 66.6667, places=2)
        # The old (wrong) formula would have given 75.0 - if the test
        # shows that, the fix didn't take.
        self.assertNotAlmostEqual(vwap, 75.0, places=2)

    def test_vwap_weighted_by_size_not_notional(self):
        """Three sub-events with same price but different notional -> VWAP = price.
        Three sub-events with different price and different notional -> true
        size-weighted average.
        """
        # Each sub-event has $1M notional at different prices
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B", notional=1_000_000, price_avg=100.0),
            _ev("2026-08-02T10:00:01+00:00", "BTC", "B", notional=2_000_000, price_avg=200.0),
            _ev("2026-08-02T10:00:02+00:00", "BTC", "B", notional=1_000_000, price_avg=50.0),
        ]
        out = cluster_events(events)
        # Total notional = 4M. Sizes: 10k + 10k + 20k = 40k. True VWAP = 4M/40k = 100.
        # OLD (wrong) formula: (100*1M + 200*2M + 50*1M) / 4M = 550/4 = 137.5
        self.assertAlmostEqual(out[0]["event_vwap"], 100.0, places=2)


class TestClusterTimingValidation(unittest.TestCase):
    """Verify cluster start_ts and end_ts are correct.

    The detector's burst_window_ms is 2s, so the first event in a cluster
    could be up to 2s AFTER the actual cascade started. The cluster's
    start_ts is the first event's ts (the detector's first detection),
    NOT the cascade's actual start. This is by design - we can only
    timestamp what we observed.
    """

    def test_start_ts_is_first_event(self):
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B"),
            _ev("2026-08-02T10:00:01+00:00", "BTC", "B"),
            _ev("2026-08-02T10:00:30+00:00", "BTC", "B"),  # 30s gap, still in window
        ]
        out = cluster_events(events, time_window_s=60)
        self.assertEqual(out[0]["start_ts"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(out[0]["end_ts"], "2026-08-02T10:00:30+00:00")

    def test_end_ts_is_last_event(self):
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B"),
            _ev("2026-08-02T10:00:05+00:00", "BTC", "B"),
            _ev("2026-08-02T10:00:10+00:00", "BTC", "B"),
        ]
        out = cluster_events(events)
        self.assertEqual(out[0]["start_ts"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(out[0]["end_ts"], "2026-08-02T10:00:10+00:00")

    def test_duration_ms(self):
        events = [
            _ev("2026-08-02T10:00:00.000+00:00", "BTC", "B"),
            _ev("2026-08-02T10:00:01.500+00:00", "BTC", "B"),
        ]
        out = cluster_events(events)
        self.assertEqual(out[0]["duration_ms"], 1500)

    def test_no_window_no_merge(self):
        # Events 5s apart, window=2s -> stay separate
        events = [
            _ev("2026-08-02T10:00:00+00:00", "BTC", "B"),
            _ev("2026-08-02T10:00:05+00:00", "BTC", "B"),
        ]
        out = cluster_events(events, time_window_s=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["end_ts"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(out[1]["start_ts"], "2026-08-02T10:00:05+00:00")


if __name__ == "__main__":
    unittest.main()

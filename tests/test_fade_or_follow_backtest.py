"""Tests for src/strategy/fade_or_follow_backtest.py."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.fade_or_follow_backtest import (
    Trade,
    _is_reclaim,
    _fade_direction,
    _continuation_direction,
    _return_pct,
    find_entry_idx,
    run_backtest,
    summarize,
)


def _bar(t_ms, o=100, h=101, l=99, c=100, n=10):
    return {"t": t_ms, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "n": n}


def _cascade(start_ts, sym="BTC", side="B", vwap=100.0, n_events=1, total_notional=1_000_000):
    return {
        "symbol": sym,
        "side": side,
        "start_ts": start_ts,
        "end_ts": start_ts,
        "event_vwap": vwap,
        "n_events": n_events,
        "total_notional": total_notional,
    }


class TestHelpers(unittest.TestCase):
    def test_is_reclaim(self):
        # B-side cascade moved price up. Reclaim = price below vwap.
        self.assertTrue(_is_reclaim("B", close=99, vwap=100))
        self.assertFalse(_is_reclaim("B", close=101, vwap=100))
        # A-side cascade moved price down. Reclaim = price above vwap.
        self.assertTrue(_is_reclaim("A", close=101, vwap=100))
        self.assertFalse(_is_reclaim("A", close=99, vwap=100))

    def test_directions(self):
        self.assertEqual(_fade_direction("B"), "short")
        self.assertEqual(_fade_direction("A"), "long")
        self.assertEqual(_continuation_direction("B"), "long")
        self.assertEqual(_continuation_direction("A"), "short")

    def test_return_pct(self):
        # Long: profit if exit > entry
        self.assertAlmostEqual(_return_pct("long", 100, 110), 10.0)
        self.assertAlmostEqual(_return_pct("long", 100, 90), -10.0)
        # Short: profit if exit < entry
        self.assertAlmostEqual(_return_pct("short", 100, 90), 10.0)
        self.assertAlmostEqual(_return_pct("short", 100, 110), -10.0)


class TestFindEntryIdx(unittest.TestCase):
    def test_finds_first_bar_at_or_after(self):
        # Cascade at 02:10:37 UTC -> entry bar is 02:11:00 (next 1m boundary)
        candles = [_bar(t) for t in [1785636600000, 1785636660000, 1785636720000, 1785636780000]]
        # 02:10:37 UTC ms = 1785636637000
        idx = find_entry_idx(candles, "2026-08-02T02:10:37+00:00")
        self.assertEqual(idx, 1)  # 02:11:00 bar

    def test_no_bar_after_returns_none(self):
        candles = [_bar(t) for t in [1785636600000, 1785636660000]]
        idx = find_entry_idx(candles, "2030-01-01T00:00:00+00:00")
        self.assertIsNone(idx)


class TestRunBacktest(unittest.TestCase):
    def test_no_candles_no_trades(self):
        c = _cascade("2026-08-02T02:10:37+00:00", vwap=100)
        self.assertEqual(run_backtest([c], {}), [])

    def test_baseline_fade_always_emitted(self):
        # Build enough 1m candles for entry + 15m exit
        # Cascade at 02:10:37 UTC, entry bar = 02:11:00 (bar index 1)
        # Exit bar = bar 16 (02:26:00), close = 100 + 16*0.1 = 101.6
        candles = [_bar(1785636600000 + i * 60000, c=str(100 + i * 0.1)) for i in range(30)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        # baseline_fade always emitted
        variants = [t.variant for t in trades]
        self.assertIn("baseline_fade", variants)
        # baseline_fade enters at next bar (02:11:00, bar 1), exits at bar 16
        bf = next(t for t in trades if t.variant == "baseline_fade")
        self.assertEqual(bf.direction, "short")
        self.assertEqual(bf.bars_held, 15)
        # entry=100.1, exit=101.6. short return = (100.1 - 101.6) / 100.1 * 100
        expected_return = (100.1 - 101.6) / 100.1 * 100
        self.assertAlmostEqual(bf.return_pct, expected_return, places=2)

    def test_reclaim_fade_only_when_reclaim(self):
        # 1m bars after cascade. B-side cascade vwap=100. Bars in window close at:
        # 100.1, 99.5 (reclaim!), 99.0
        candles = []
        for i in range(30):
            # i=0 = bar at 02:11:00
            if i == 0:
                c = 100.1
            elif i == 1:
                c = 99.5  # RECLAIM
            else:
                c = 99.0
            candles.append(_bar(1785636600000 + i * 60000, c=c))
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        rf = [t for t in trades if t.variant == "reclaim_fade"]
        self.assertEqual(len(rf), 1)
        self.assertEqual(rf[0].direction, "short")
        # Entered at reclaim bar (i=1, close 99.5)
        self.assertAlmostEqual(rf[0].entry_price, 99.5)
        self.assertTrue(rf[0].reclaim_detected)

    def test_failed_reclaim_continuation_only_when_no_reclaim(self):
        # B-side vwap=100. All bars in window close ABOVE 100 -> no reclaim.
        # Wait window = bars [1..4] inclusive (entry + 3 min).
        # End of wait = bar 4, close = 101.0 + 4*0.1 = 101.4
        candles = [_bar(1785636600000 + i * 60000, c=str(101.0 + i * 0.1)) for i in range(30)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        cont = [t for t in trades if t.variant == "failed_reclaim_continuation"]
        self.assertEqual(len(cont), 1)
        # Continuation on B-side = LONG
        self.assertEqual(cont[0].direction, "long")
        # End of wait = bar 4 (entry_idx=1 + wait_minutes=3), close = 101.4
        self.assertAlmostEqual(cont[0].entry_price, 101.4, places=2)

    def test_no_continuation_when_reclaim(self):
        # If reclaim IS detected, no continuation trade.
        candles = [_bar(1785636600000 + i * 60000, c=str(99.5)) for i in range(30)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        cont = [t for t in trades if t.variant == "failed_reclaim_continuation"]
        self.assertEqual(len(cont), 0)

    def test_skip_when_no_exit_bar(self):
        # Only 5 bars total, exit_idx = entry + 15 would be out of range
        candles = [_bar(1785636600000 + i * 60000, c=str(100)) for i in range(5)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        self.assertEqual(len(trades), 0)


class TestSummarize(unittest.TestCase):
    def test_groups_by_variant_and_symbol(self):
        t1 = Trade("ts", "BTC", "B", "baseline_fade", "short", 100, "t1", 100, "t2", 90, 10.0, 15, False, "x")
        t2 = Trade("ts", "BTC", "B", "baseline_fade", "short", 100, "t1", 100, "t2", 110, -10.0, 15, False, "x")
        t3 = Trade("ts", "ETH", "B", "baseline_fade", "short", 100, "t1", 100, "t2", 105, 5.0, 15, False, "x")
        s = summarize([t1, t2, t3])
        self.assertIn("baseline_fade|BTC", s)
        btc = s["baseline_fade|BTC"]
        self.assertEqual(btc["n"], 2)
        self.assertEqual(btc["win_rate"], 0.5)
        self.assertEqual(btc["avg_return_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()

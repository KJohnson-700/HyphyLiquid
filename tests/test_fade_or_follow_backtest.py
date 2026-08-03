"""Tests for src/strategy/fade_or_follow_backtest.py."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.fade_or_follow_backtest import (
    Trade,
    _continuation_direction,
    _fade_direction,
    _is_reclaim,
    _return_pct,
    find_entry_idx,
    run_backtest,
    summarize,
)
from scripts.run_fade_or_follow_backtest import _load_candles


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _bar(t_ms: int, o=100, h=101, l=99, c=100, n=10) -> dict:
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
        self.assertTrue(_is_reclaim("B", close=99, vwap=100))
        self.assertFalse(_is_reclaim("B", close=101, vwap=100))
        self.assertTrue(_is_reclaim("A", close=101, vwap=100))
        self.assertFalse(_is_reclaim("A", close=99, vwap=100))

    def test_directions(self):
        self.assertEqual(_fade_direction("B"), "short")
        self.assertEqual(_fade_direction("A"), "long")
        self.assertEqual(_continuation_direction("B"), "long")
        self.assertEqual(_continuation_direction("A"), "short")

    def test_return_pct(self):
        self.assertAlmostEqual(_return_pct("long", 100, 110), 10.0)
        self.assertAlmostEqual(_return_pct("long", 100, 90), -10.0)
        self.assertAlmostEqual(_return_pct("short", 100, 90), 10.0)
        self.assertAlmostEqual(_return_pct("short", 100, 110), -10.0)


class TestFindEntryIdx(unittest.TestCase):
    def test_finds_first_bar_after_event(self):
        candles = [
            _bar(_ms("2026-08-02T02:10:00+00:00")),
            _bar(_ms("2026-08-02T02:11:00+00:00")),
            _bar(_ms("2026-08-02T02:12:00+00:00")),
        ]
        idx = find_entry_idx(candles, "2026-08-02T02:10:37+00:00")
        self.assertEqual(idx, 1)

    def test_no_bar_after_returns_none(self):
        candles = [_bar(_ms("2026-08-02T02:10:00+00:00"))]
        idx = find_entry_idx(candles, "2030-01-01T00:00:00+00:00")
        self.assertIsNone(idx)

    def test_skips_when_first_candle_is_too_late(self):
        candles = [_bar(_ms("2026-08-02T12:00:00+00:00"))]
        idx = find_entry_idx(
            candles,
            "2026-08-02T11:00:00+00:00",
            max_entry_lag_minutes=2,
        )
        self.assertIsNone(idx)

    def test_allows_next_minute_entry(self):
        candles = [
            _bar(_ms("2026-08-02T11:01:00+00:00")),
            _bar(_ms("2026-08-02T11:02:00+00:00")),
        ]
        idx = find_entry_idx(
            candles,
            "2026-08-02T11:00:30+00:00",
            max_entry_lag_minutes=2,
        )
        self.assertEqual(idx, 0)


class TestLoadCandles(unittest.TestCase):
    def test_loads_all_dates_and_keeps_last_update_per_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            candle_dir = Path(tmp)
            rows_0802 = [
                {
                    "payload": {
                        "t": 1785715200000,
                        "s": "DOGE",
                        "o": "0.070",
                        "h": "0.071",
                        "l": "0.069",
                        "c": "0.0701",
                        "v": "10",
                        "n": 1,
                    }
                },
                {
                    "payload": {
                        "t": 1785715200000,
                        "s": "DOGE",
                        "o": "0.070",
                        "h": "0.072",
                        "l": "0.069",
                        "c": "0.0702",
                        "v": "20",
                        "n": 2,
                    }
                },
            ]
            rows_0803 = [
                {
                    "payload": {
                        "t": 1785715260000,
                        "s": "DOGE",
                        "o": "0.0702",
                        "h": "0.073",
                        "l": "0.070",
                        "c": "0.0704",
                        "v": "30",
                        "n": 3,
                    }
                }
            ]
            (candle_dir / "doge_2026-08-02.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows_0802),
                encoding="utf-8",
            )
            (candle_dir / "doge_2026-08-03.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows_0803),
                encoding="utf-8",
            )

            candles = _load_candles("DOGE", candle_dir)

        self.assertEqual([c["t"] for c in candles], [1785715200000, 1785715260000])
        self.assertEqual(candles[0]["c"], 0.0702)
        self.assertEqual(candles[0]["n"], 2)


class TestRunBacktest(unittest.TestCase):
    def test_no_candles_no_trades(self):
        c = _cascade("2026-08-02T02:10:37+00:00", vwap=100)
        self.assertEqual(run_backtest([c], {}), [])

    def test_baseline_fade_always_emitted(self):
        candles = [
            _bar(_ms("2026-08-02T02:10:00+00:00") + i * 60000, c=100 + i * 0.1)
            for i in range(30)
        ]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        variants = [t.variant for t in trades]
        self.assertIn("baseline_fade", variants)
        bf = next(t for t in trades if t.variant == "baseline_fade")
        self.assertEqual(bf.direction, "short")
        self.assertEqual(bf.bars_held, 15)
        self.assertEqual(bf.entry_lag_s, 23.0)
        expected_return = (100.1 - 101.6) / 100.1 * 100
        self.assertAlmostEqual(bf.return_pct, expected_return, places=2)

    def test_reclaim_fade_only_when_reclaim(self):
        candles = []
        base = _ms("2026-08-02T02:10:00+00:00")
        for i in range(30):
            close = 100.1 if i == 1 else 99.5 if i == 2 else 99.0
            candles.append(_bar(base + i * 60000, c=close))
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        rf = [t for t in trades if t.variant == "reclaim_fade"]
        self.assertEqual(len(rf), 1)
        self.assertEqual(rf[0].direction, "short")
        self.assertAlmostEqual(rf[0].entry_price, 99.5)
        self.assertTrue(rf[0].reclaim_detected)

    def test_failed_reclaim_continuation_only_when_no_reclaim(self):
        base = _ms("2026-08-02T02:10:00+00:00")
        candles = [_bar(base + i * 60000, c=101.0 + i * 0.1) for i in range(30)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        cont = [t for t in trades if t.variant == "failed_reclaim_continuation"]
        self.assertEqual(len(cont), 1)
        self.assertEqual(cont[0].direction, "long")
        self.assertAlmostEqual(cont[0].entry_price, 101.4, places=2)

    def test_no_continuation_when_reclaim(self):
        base = _ms("2026-08-02T02:10:00+00:00")
        candles = [_bar(base + i * 60000, c=99.5) for i in range(30)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        cont = [t for t in trades if t.variant == "failed_reclaim_continuation"]
        self.assertEqual(len(cont), 0)

    def test_skip_when_no_exit_bar(self):
        base = _ms("2026-08-02T02:10:00+00:00")
        candles = [_bar(base + i * 60000, c=100) for i in range(5)]
        c = _cascade("2026-08-02T02:10:37+00:00", side="B", vwap=100.0)
        trades = run_backtest([c], {"BTC": candles}, horizon_minutes=15, wait_minutes=3)
        self.assertEqual(len(trades), 0)


class TestSummarize(unittest.TestCase):
    def test_groups_by_variant_and_symbol(self):
        t1 = Trade("ts", "BTC", "B", "baseline_fade", "short", 100, "t1", 100, "t2", 90, 10.0, 15, 1.0, False, "x")
        t2 = Trade("ts", "BTC", "B", "baseline_fade", "short", 100, "t1", 100, "t2", 110, -10.0, 15, 1.0, False, "x")
        t3 = Trade("ts", "ETH", "B", "baseline_fade", "short", 100, "t1", 100, "t2", 105, 5.0, 15, 1.0, False, "x")
        s = summarize([t1, t2, t3])
        self.assertIn("baseline_fade|BTC", s)
        btc = s["baseline_fade|BTC"]
        self.assertEqual(btc["n"], 2)
        self.assertEqual(btc["win_rate"], 0.5)
        self.assertEqual(btc["avg_return_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()

"""Tests for lane-aware backtesting."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.lane_backtest import (  # noqa: E402
    _range_confirmation,
    apply_r_multiple_exits,
    atr_at,
    bollinger_at,
    diagnostic_breakdown,
    apply_trailing_exits,
    run_alt_range_liq_scalp,
    simulate_r_multiple_exit,
    simulate_trailing_stop_exit,
    summarize_exit_analysis,
    summarize_trailing_analysis,
    summarize_lane_trades,
)


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _bar(t_ms: int, o=100, h=101, l=99, c=100, v=1000, n=10) -> dict:
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v, "n": n}


def _cascade(start_ts: str, sym="SOL", side="B") -> dict:
    return {
        "symbol": sym,
        "side": side,
        "start_ts": start_ts,
        "event_vwap": 100.0,
        "total_notional": 100_000,
    }


class TestBands(unittest.TestCase):
    def test_bollinger_uses_prior_bars_only(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [_bar(base + i * 60000, c=100) for i in range(20)]
        candles.append(_bar(base + 20 * 60000, c=500))

        bands = bollinger_at(candles, 20, period=20)

        self.assertIsNotNone(bands)
        assert bands is not None
        self.assertEqual(bands["mid"], 100)
        self.assertEqual(bands["upper"], 100)
        self.assertEqual(bands["lower"], 100)

    def test_range_confirmation_for_upper_and_lower_band(self):
        upper = {"upper": 105.0, "lower": 95.0, "mid": 100.0}
        self.assertTrue(_range_confirmation("B", _bar(1, h=106, c=104), upper))
        self.assertFalse(_range_confirmation("B", _bar(1, h=104, c=103), upper))
        self.assertTrue(_range_confirmation("A", _bar(1, l=94, c=96), upper))
        self.assertFalse(_range_confirmation("A", _bar(1, l=96, c=97), upper))


class TestAltRangeLiqScalp(unittest.TestCase):
    def test_short_fade_targets_mid_band(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        closes = [98, 99, 100, 101, 102] * 4
        candles = [
            _bar(base + i * 60000, c=close, h=close + 0.5, l=close - 0.5)
            for i, close in enumerate(closes)
        ]
        candles.append(_bar(base + 20 * 60000, o=102, h=106, l=100, c=102))
        candles.append(_bar(base + 21 * 60000, o=102, h=102.5, l=99, c=100))
        cascade = _cascade("2026-08-03T00:19:30+00:00", side="B")

        trades = run_alt_range_liq_scalp(
            [cascade],
            {"SOL": candles},
            band_period=20,
            stop_buffer_bps=5,
            round_trip_cost_bps=8,
        )

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].direction, "short")
        self.assertEqual(trades[0].exit_reason, "mid_band_target")
        self.assertAlmostEqual(trades[0].gross_return_pct, (102 - 100) / 102 * 100, places=4)
        self.assertAlmostEqual(trades[0].net_return_pct, trades[0].gross_return_pct - 0.08, places=4)

    def test_long_fade_stops_beyond_lower_band(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        closes = [98, 99, 100, 101, 102] * 4
        candles = [
            _bar(base + i * 60000, c=close, h=close + 0.5, l=close - 0.5)
            for i, close in enumerate(closes)
        ]
        candles.append(_bar(base + 20 * 60000, o=98, h=100, l=94, c=98))
        candles.append(_bar(base + 21 * 60000, o=98, h=98, l=96, c=97))
        cascade = _cascade("2026-08-03T00:19:30+00:00", side="A")

        trades = run_alt_range_liq_scalp([cascade], {"SOL": candles}, band_period=20)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].direction, "long")
        self.assertEqual(trades[0].exit_reason, "stop")

    def test_refuses_btc_eth_by_default(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [_bar(base + i * 60000, c=100, h=100.5, l=99.5) for i in range(22)]
        cascade = _cascade("2026-08-03T00:19:30+00:00", sym="BTC", side="B")

        trades = run_alt_range_liq_scalp([cascade], {"BTC": candles}, band_period=20)

        self.assertEqual(trades, [])

    def test_summarize_lane_trades(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        closes = [98, 99, 100, 101, 102] * 4
        candles = [
            _bar(base + i * 60000, c=close, h=close + 0.5, l=close - 0.5)
            for i, close in enumerate(closes)
        ]
        candles.extend([
            _bar(base + 20 * 60000, h=106, l=100, c=102),
            _bar(base + 21 * 60000, h=102.5, l=99, c=100),
        ])
        trades = run_alt_range_liq_scalp(
            [_cascade("2026-08-03T00:19:30+00:00")],
            {"SOL": candles},
            band_period=20,
        )

        summary = summarize_lane_trades(trades)

        self.assertIn("alt_range_liq_scalp|SOL", summary)
        self.assertEqual(summary["alt_range_liq_scalp|SOL"]["n"], 1)


class TestRMultipleExits(unittest.TestCase):
    def test_long_target_hit(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=100.4, l=99.95, c=100.25),
        ]

        out = simulate_r_multiple_exit(
            candles,
            entry_idx=0,
            direction="long",
            entry_price=100,
            stop_bps=10,
            target_r=2.0,
            max_hold_minutes=5,
            round_trip_cost_bps=0,
        )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["exit_reason"], "target_2r")
        self.assertAlmostEqual(out["gross_return_pct"], 0.2)
        self.assertAlmostEqual(out["r_multiple"], 2.0)

    def test_same_bar_stop_beats_target(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=100.3, l=99.8, c=100.1),
        ]

        out = simulate_r_multiple_exit(
            candles,
            entry_idx=0,
            direction="long",
            entry_price=100,
            stop_bps=10,
            target_r=2.0,
            max_hold_minutes=5,
            round_trip_cost_bps=0,
        )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["exit_reason"], "stop")
        self.assertAlmostEqual(out["gross_return_pct"], -0.1)
        self.assertAlmostEqual(out["mae_pct"], 0.2)
        self.assertAlmostEqual(out["mfe_pct"], 0.3)

    def test_short_timeout_tracks_mae_mfe_and_costs(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=100.05, l=99.95, c=99.98),
            _bar(base + 120000, h=100.04, l=99.90, c=99.96),
        ]

        out = simulate_r_multiple_exit(
            candles,
            entry_idx=0,
            direction="short",
            entry_price=100,
            stop_bps=20,
            target_r=2.5,
            max_hold_minutes=2,
            round_trip_cost_bps=8,
        )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["exit_reason"], "timeout")
        self.assertAlmostEqual(out["gross_return_pct"], 0.04)
        self.assertAlmostEqual(out["net_return_pct"], -0.04)
        self.assertAlmostEqual(out["r_multiple"], -0.2)
        self.assertAlmostEqual(out["mfe_pct"], 0.10)
        self.assertAlmostEqual(out["mae_pct"], 0.05)

    def test_apply_r_multiple_exits_rescores_serialized_trades(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=100.4, l=99.95, c=100.25),
        ]
        trades = [{
            "lane": "btc_eth_fade_or_follow",
            "cascade_start_ts": "2026-08-02T23:59:30+00:00",
            "symbol": "BTC",
            "side": "A",
            "variant": "baseline_fade",
            "direction": "long",
            "entry_ts": "2026-08-03T00:00:00+00:00",
            "entry_price": 100,
            "exit_reason": "fixed_horizon",
        }]

        rescored = apply_r_multiple_exits(
            trades,
            {"BTC": candles},
            stop_bps=10,
            target_r=2.0,
            max_hold_minutes=5,
            round_trip_cost_bps=0,
        )
        summary = summarize_exit_analysis(rescored)

        self.assertEqual(len(rescored), 1)
        self.assertEqual(rescored[0].exit_reason, "target_2r")
        self.assertEqual(summary["btc_eth_fade_or_follow|baseline_fade|BTC"]["n"], 1)
        self.assertEqual(summary["btc_eth_fade_or_follow|baseline_fade|BTC"]["target_rate"], 1.0)

    def test_atr_stop_uses_prior_completed_bars(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base + i * 60000, h=101, l=99, c=100)
            for i in range(15)
        ]
        candles.append(_bar(base + 15 * 60000, h=110, l=90, c=100))
        trades = [{
            "lane": "btc_eth_fade_or_follow",
            "cascade_start_ts": "2026-08-02T23:59:30+00:00",
            "symbol": "BTC",
            "side": "A",
            "variant": "baseline_fade",
            "direction": "long",
            "entry_ts": "2026-08-03T00:15:00+00:00",
            "entry_price": 100,
        }]

        self.assertAlmostEqual(atr_at(candles, 15, period=14), 2.0)
        rescored = apply_r_multiple_exits(
            trades,
            {"BTC": candles + [_bar(base + 16 * 60000, h=101, l=97, c=98)]},
            stop_bps=0,
            target_r=1.0,
            max_hold_minutes=2,
            round_trip_cost_bps=0,
            stop_model="atr",
            atr_period=14,
            atr_mult=1.0,
        )

        self.assertEqual(len(rescored), 1)
        self.assertEqual(rescored[0].stop_model, "atr")
        self.assertAlmostEqual(rescored[0].stop_bps, 200.0)
        self.assertEqual(rescored[0].exit_reason, "stop")

    def test_event_vwap_stop_skips_wrong_side_stop(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=100.2, l=99.8, c=100),
        ]
        trades = [{
            "lane": "btc_eth_fade_or_follow",
            "cascade_start_ts": "2026-08-02T23:59:30+00:00",
            "symbol": "BTC",
            "side": "A",
            "variant": "baseline_fade",
            "direction": "long",
            "entry_ts": "2026-08-03T00:00:00+00:00",
            "entry_price": 100,
            "event_vwap": 101,
        }]

        rescored = apply_r_multiple_exits(
            trades,
            {"BTC": candles},
            stop_bps=0,
            target_r=1.0,
            max_hold_minutes=1,
            round_trip_cost_bps=0,
            stop_model="event_vwap",
        )

        self.assertEqual(rescored, [])

    def test_event_vwap_stop_derives_effective_stop_distance(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=101, l=98.9, c=99),
        ]
        trades = [{
            "lane": "btc_eth_fade_or_follow",
            "cascade_start_ts": "2026-08-02T23:59:30+00:00",
            "symbol": "BTC",
            "side": "A",
            "variant": "baseline_fade",
            "direction": "long",
            "entry_ts": "2026-08-03T00:00:00+00:00",
            "entry_price": 100,
            "event_vwap": 99,
        }]

        rescored = apply_r_multiple_exits(
            trades,
            {"BTC": candles},
            stop_bps=0,
            target_r=1.0,
            max_hold_minutes=1,
            round_trip_cost_bps=0,
            stop_model="event_vwap",
            vwap_buffer_bps=0,
        )

        self.assertEqual(len(rescored), 1)
        self.assertEqual(rescored[0].stop_model, "event_vwap")
        self.assertAlmostEqual(rescored[0].stop_bps, 100.0)
        self.assertEqual(rescored[0].exit_reason, "stop")

    def test_trailing_stop_activates_then_exits(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=101.2, l=100.2, c=101),
            _bar(base + 120000, h=101.3, l=100.8, c=100.9),
        ]

        out = simulate_trailing_stop_exit(
            candles,
            entry_idx=0,
            direction="long",
            entry_price=100,
            initial_stop_bps=50,
            activation_r=1.0,
            trail_bps=20,
            max_hold_minutes=5,
            round_trip_cost_bps=0,
        )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["exit_reason"], "trailing_stop")
        self.assertAlmostEqual(out["exit_price"], 101.2 * (1 - 0.002))
        self.assertGreater(out["r_multiple"], 1.0)

    def test_trailing_initial_stop_before_activation(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=100.2, l=99.4, c=99.5),
        ]

        out = simulate_trailing_stop_exit(
            candles,
            entry_idx=0,
            direction="long",
            entry_price=100,
            initial_stop_bps=50,
            activation_r=1.0,
            trail_bps=20,
            max_hold_minutes=5,
            round_trip_cost_bps=0,
        )

        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["exit_reason"], "initial_stop")
        self.assertAlmostEqual(out["gross_return_pct"], -0.5)

    def test_apply_trailing_exits_summarizes_activation(self):
        base = _ms("2026-08-03T00:00:00+00:00")
        candles = [
            _bar(base, h=100.1, l=99.9, c=100),
            _bar(base + 60000, h=101.2, l=100.2, c=101),
            _bar(base + 120000, h=101.3, l=100.8, c=100.9),
        ]
        trades = [{
            "lane": "btc_eth_trailing_resolution",
            "cascade_start_ts": "2026-08-02T23:59:30+00:00",
            "symbol": "BTC",
            "side": "A",
            "variant": "baseline_fade",
            "direction": "long",
            "entry_ts": "2026-08-03T00:00:00+00:00",
            "entry_price": 100,
        }]

        rescored = apply_trailing_exits(
            trades,
            {"BTC": candles},
            initial_stop_bps=50,
            activation_r=1.0,
            trail_bps=20,
            max_hold_minutes=5,
            round_trip_cost_bps=0,
        )
        summary = summarize_trailing_analysis(rescored)

        self.assertEqual(len(rescored), 1)
        row = summary["btc_eth_trailing_resolution|baseline_fade|BTC"]
        self.assertEqual(row["activation_rate"], 1.0)
        self.assertEqual(row["trailing_stop_rate"], 1.0)


class TestDiagnostics(unittest.TestCase):
    def test_diagnostic_breakdown_groups_by_side_and_outlier_share(self):
        trades = [
            {"symbol": "HYPE", "side": "B", "direction": "short", "net_return_pct": 1.0, "band_width_pct": 0.4},
            {"symbol": "HYPE", "side": "B", "direction": "short", "net_return_pct": 0.5, "band_width_pct": 0.7},
            {"symbol": "HYPE", "side": "A", "direction": "long", "net_return_pct": -0.5, "band_width_pct": 1.2},
        ]

        out = diagnostic_breakdown(trades, return_field="net_return_pct", include_band_buckets=True)

        self.assertEqual(out["all"]["n"], 3)
        self.assertEqual(out["side=B"]["n"], 2)
        self.assertEqual(out["symbol=HYPE|side=A"]["profit_factor"], 0.0)
        self.assertAlmostEqual(out["all"]["largest_win_share_of_gross_profit"], 0.6667)
        self.assertEqual(out["band_width=compressed"]["n"], 1)
        self.assertEqual(out["band_width=normal"]["n"], 1)
        self.assertEqual(out["band_width=wide"]["n"], 1)


if __name__ == "__main__":
    unittest.main()

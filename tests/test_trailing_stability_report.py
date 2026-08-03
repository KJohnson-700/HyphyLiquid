"""Tests for trailing-stability report helpers."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_trailing_stability_report import summarize_trailing_rows  # noqa: E402
from src.strategy.lane_backtest import TrailingExitTrade  # noqa: E402


def _trade(return_pct: float, reason: str, start: str = "2026-08-03T00:00:00+00:00") -> TrailingExitTrade:
    return TrailingExitTrade(
        lane="btc_eth_trailing_resolution",
        cascade_start_ts=start,
        symbol="BTC",
        side="B",
        variant="failed_reclaim_continuation",
        direction="long",
        entry_ts=start,
        entry_price=100.0,
        exit_ts=start,
        exit_price=101.0,
        gross_return_pct=return_pct,
        net_return_pct=return_pct,
        r_multiple=return_pct,
        stop_model="event_vwap",
        initial_stop_bps=25.0,
        activation_r=2.0,
        trail_bps=10.0,
        initial_stop_price=99.0,
        activation_price=102.0,
        final_trailing_stop=101.0,
        mae_pct=0.1,
        mfe_pct=0.3,
        bars_held=10,
        exit_reason=reason,
    )


class TestTrailingStabilityReport(unittest.TestCase):
    def test_summarize_trailing_rows_counts_activation_and_pf(self):
        rows = [
            _trade(0.30, "trailing_stop", "2026-08-03T00:00:00+00:00"),
            _trade(0.10, "timeout_trailing_active", "2026-08-03T01:00:00+00:00"),
            _trade(-0.20, "initial_stop", "2026-08-03T02:00:00+00:00"),
        ]

        out = summarize_trailing_rows(rows)

        self.assertEqual(out["n"], 3)
        self.assertAlmostEqual(out["win_rate"], 0.6667)
        self.assertEqual(out["profit_factor"], 2.0)
        self.assertAlmostEqual(out["activation_rate"], 0.6667)
        self.assertAlmostEqual(out["initial_stop_rate"], 0.3333)
        self.assertAlmostEqual(out["trailing_stop_rate"], 0.3333)
        self.assertEqual(out["first_event_ts"], "2026-08-03T00:00:00+00:00")
        self.assertEqual(out["last_event_ts"], "2026-08-03T02:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

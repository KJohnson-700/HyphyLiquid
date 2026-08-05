"""Tests for research-only event range grid backtest."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.grid_backtest import (  # noqa: E402
    GridConfig,
    run_event_range_grid,
    simulate_grid_trade,
    summarize_grid_trades,
)


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _bar(t_ms: int, c: float, h: float | None = None, l: float | None = None) -> dict:
    return {
        "t": t_ms,
        "o": c,
        "h": h if h is not None else c + 0.1,
        "l": l if l is not None else c - 0.1,
        "c": c,
    }


class TestGridBacktest(unittest.TestCase):
    def test_b_side_upper_sweep_short_grid_hits_mid_target(self):
        base = _ms("2026-08-05T00:00:00+00:00")
        closes = [100, 101, 99, 100.5, 99.5] * 4
        candles = [_bar(base + i * 60_000, c, h=c + 0.4, l=c - 0.4) for i, c in enumerate(closes)]
        candles.append(_bar(base + 20 * 60_000, 101.45, h=101.6, l=101.2))
        candles.append(_bar(base + 21 * 60_000, 100.0, h=101.2, l=99.8))
        cascade = {
            "symbol": "HYPE",
            "side": "B",
            "start_ts": "2026-08-05T00:20:00+00:00",
        }

        trade = simulate_grid_trade(
            cascade,
            candles,
            20,
            GridConfig(grid_spacing_bps=10, max_levels=3, allowed_band_buckets=("very_wide",)),
        )

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.direction, "short")
        self.assertEqual(trade.exit_reason, "mid_band_target")
        self.assertGreater(trade.net_return_pct, 0)

    def test_rejects_research_symbol_outside_grid_scope(self):
        base = _ms("2026-08-05T00:00:00+00:00")
        candles = [_bar(base + i * 60_000, 100) for i in range(25)]
        cascade = {"symbol": "BTC", "side": "B", "start_ts": "2026-08-05T00:20:00+00:00"}

        self.assertIsNone(simulate_grid_trade(cascade, candles, 20, GridConfig()))

    def test_rejects_disallowed_band_bucket(self):
        base = _ms("2026-08-05T00:00:00+00:00")
        candles = [_bar(base + i * 60_000, 100, h=100.01, l=99.99) for i in range(20)]
        candles.append(_bar(base + 20 * 60_000, 100.02, h=100.03, l=99.99))
        cascade = {"symbol": "HYPE", "side": "B", "start_ts": "2026-08-05T00:20:00+00:00"}

        self.assertIsNone(
            simulate_grid_trade(cascade, candles, 20, GridConfig(allowed_band_buckets=("normal", "wide")))
        )

    def test_run_and_summarize_grid_trades(self):
        base = _ms("2026-08-05T00:00:00+00:00")
        closes = [100, 101, 99, 100.5, 99.5] * 4
        candles = [_bar(base + i * 60_000, c, h=c + 0.4, l=c - 0.4) for i, c in enumerate(closes)]
        candles.extend([
            _bar(base + 20 * 60_000, 101.45, h=101.6, l=101.2),
            _bar(base + 21 * 60_000, 100.0, h=101.2, l=99.8),
        ])
        cascades = [{"symbol": "HYPE", "side": "B", "start_ts": "2026-08-05T00:19:59+00:00"}]

        trades = run_event_range_grid(cascades, {"HYPE": candles}, GridConfig(allowed_band_buckets=("very_wide",)))
        summary = summarize_grid_trades(trades)

        self.assertEqual(len(trades), 1)
        self.assertIn("symbol=HYPE", summary)
        self.assertEqual(summary["symbol=HYPE"]["n"], 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the live-like paper decision loop."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.paper_decision_loop import run_once  # noqa: E402
from src.execution.paper_broker import PaperBracket, PaperPosition, mark_position  # noqa: E402


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _bar(t_ms: int, c: float, h: float | None = None, l: float | None = None) -> dict:
    return {
        "t": t_ms,
        "T": t_ms + 59_999,
        "o": str(c),
        "h": str(h if h is not None else c + 0.01),
        "l": str(l if l is not None else c - 0.01),
        "c": str(c),
        "s": "BTC",
        "i": "1m",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class TestPaperBroker(unittest.TestCase):
    def test_same_bar_initial_stop_beats_target_with_slippage(self):
        candles = [
            _bar(_ms("2026-08-04T00:00:00+00:00"), 100),
            _bar(_ms("2026-08-04T00:01:00+00:00"), 100, h=101, l=99),
        ]
        position = PaperPosition(
            paper_id="paper-test",
            paper_scope="research_paper",
            cascade_key="k",
            symbol="HYPE",
            side="B",
            lane="alt_range_liq_scalp",
            direction="short",
            event_ts="2026-08-04T00:00:00+00:00",
            entry_ts="2026-08-04T00:00:00+00:00",
            entry_idx=0,
            entry_price=100,
            notional_usd=1000,
            risk_usd=5,
            bracket=PaperBracket(
                entry_price=100,
                initial_stop_price=100.5,
                target_price=99.5,
                activation_price=None,
                trail_bps=None,
                max_hold_minutes=5,
                stop_slippage_bps=2,
                round_trip_cost_bps=0,
            ),
            metadata={},
        )

        fill = mark_position(position, candles)

        self.assertEqual(fill.status, "closed")
        self.assertEqual(fill.exit_reason, "initial_stop")
        self.assertGreater(fill.exit_price, 100.5)
        self.assertLess(fill.net_return_pct, -0.5)


class TestPaperDecisionLoop(unittest.TestCase):
    def test_script_does_not_import_live_exchange_boundary(self):
        source = (Path(__file__).resolve().parent.parent / "scripts" / "paper_decision_loop.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("from src.execution.order_manager", source)
        self.assertNotIn("import src.execution.order_manager", source)
        self.assertNotIn("src.exchange.hyperliquid", source)

    def test_run_once_opens_and_marks_btc_and_hype_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state_path = data_dir / ".paper_decision_state.json"
            btc_base = _ms("2026-08-04T00:00:00+00:00")
            btc_candles = [_bar(btc_base + i * 60_000, 100 + i * 0.2) for i in range(25)]
            btc_candles.extend([
                _bar(btc_base + 25 * 60_000, 106.0, h=106.5, l=105.8),
                _bar(btc_base + 26 * 60_000, 106.4, h=106.5, l=106.3),
            ])
            _write_jsonl(data_dir / "ws_candle" / "btc_2026-08-04.jsonl", [{"payload": c} for c in btc_candles])

            hype_base = _ms("2026-08-04T01:00:00+00:00")
            hype_closes = [100.0, 100.4, 100.0, 100.0] * 5
            hype_candles = [_bar(hype_base + i * 60_000, c) for i, c in enumerate(hype_closes)]
            hype_candles.extend([
                _bar(hype_base + 20 * 60_000, 100.3, h=100.8, l=100.2),
                _bar(hype_base + 21 * 60_000, 100.0, h=100.4, l=99.8),
            ])
            _write_jsonl(data_dir / "ws_candle" / "hype_2026-08-04.jsonl", [{"payload": c} for c in hype_candles])

            cascades = [
                {
                    "symbol": "BTC",
                    "side": "B",
                    "start_ts": "2026-08-04T00:20:30+00:00",
                    "event_vwap": 104.0,
                    "n_fills": 12,
                    "total_notional": 500000,
                },
                {
                    "symbol": "HYPE",
                    "side": "B",
                    "start_ts": "2026-08-04T01:19:30+00:00",
                    "event_vwap": 100.4,
                    "n_fills": 9,
                    "total_notional": 75000,
                },
                {
                    "symbol": "ETH",
                    "side": "B",
                    "start_ts": "2026-08-04T00:20:30+00:00",
                    "event_vwap": 100,
                    "n_fills": 3,
                    "total_notional": 50000,
                },
            ]
            _write_jsonl(data_dir / "cascades.jsonl", cascades)

            result = run_once(data_dir=data_dir, state_path=state_path, max_new=10)

            self.assertEqual(result["positions_opened"], 2)
            self.assertEqual(result["positions_closed"], 2)
            decisions = list(data_dir.glob("paper_decisions_*.jsonl"))
            positions = list(data_dir.glob("paper_positions_*.jsonl"))
            self.assertEqual(len(decisions), 1)
            self.assertEqual(len(positions), 1)
            decision_rows = [json.loads(line) for line in decisions[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["symbol"] for row in decision_rows}, {"BTC", "HYPE"})
            self.assertIn("v1_paper", {row["paper_scope"] for row in decision_rows})
            self.assertIn("research_paper", {row["paper_scope"] for row in decision_rows})

            second = run_once(data_dir=data_dir, state_path=state_path, max_new=10)
            self.assertEqual(second["positions_opened"], 0)


if __name__ == "__main__":
    unittest.main()

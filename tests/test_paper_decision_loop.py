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

    def test_open_position_marks_unrealized_pnl(self):
        candles = [
            _bar(_ms("2026-08-04T00:00:00+00:00"), 100),
            _bar(_ms("2026-08-04T00:01:00+00:00"), 100.5, h=100.6, l=100.2),
            _bar(_ms("2026-08-04T00:02:00+00:00"), 101.0, h=101.1, l=100.7),
        ]
        position = PaperPosition(
            paper_id="paper-open",
            paper_scope="v1_paper",
            cascade_key="k",
            symbol="BTC",
            side="B",
            lane="btc_eth_trailing_resolution",
            direction="long",
            event_ts="2026-08-04T00:00:00+00:00",
            entry_ts="2026-08-04T00:00:00+00:00",
            entry_idx=0,
            entry_price=100,
            notional_usd=1000,
            risk_usd=5,
            bracket=PaperBracket(
                entry_price=100,
                initial_stop_price=99,
                target_price=None,
                activation_price=105,
                trail_bps=10,
                max_hold_minutes=5,
                stop_slippage_bps=2,
                round_trip_cost_bps=8,
            ),
            metadata={},
        )

        fill = mark_position(position, candles)

        self.assertEqual(fill.status, "open")
        self.assertEqual(fill.exit_price, 101.0)
        self.assertEqual(fill.bars_held, 2)
        self.assertGreater(fill.net_return_pct, 0.9)

    def test_missing_entry_candle_stays_open_without_crashing(self):
        position = PaperPosition(
            paper_id="paper-missing",
            paper_scope="v1_paper",
            cascade_key="k",
            symbol="BTC",
            side="B",
            lane="btc_eth_trailing_resolution",
            direction="long",
            event_ts="2026-08-04T00:00:00+00:00",
            entry_ts="2026-08-04T00:10:00+00:00",
            entry_idx=10,
            entry_price=100,
            notional_usd=1000,
            risk_usd=5,
            bracket=PaperBracket(
                entry_price=100,
                initial_stop_price=99,
                target_price=None,
                activation_price=105,
                trail_bps=10,
                max_hold_minutes=5,
                stop_slippage_bps=2,
                round_trip_cost_bps=8,
            ),
            metadata={},
        )

        fill = mark_position(position, [_bar(_ms("2026-08-04T00:00:00+00:00"), 100)])

        self.assertEqual(fill.status, "open")
        self.assertIsNone(fill.exit_price)
        self.assertEqual(fill.final_trailing_stop, 99)


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
                    "top_book_imbalance": -0.5,
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

    def test_btc_filtered_gate_rejects_non_ask_heavy_book(self):
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
            _write_jsonl(
                data_dir / "cascades.jsonl",
                [
                    {
                        "symbol": "BTC",
                        "side": "B",
                        "start_ts": "2026-08-04T00:20:30+00:00",
                        "event_vwap": 104.0,
                        "n_fills": 12,
                        "total_notional": 500000,
                        "top_book_imbalance": 0.1,
                    }
                ],
            )

            result = run_once(data_dir=data_dir, state_path=state_path, max_new=10)

            self.assertEqual(result["positions_opened"], 0)
            decisions = list(data_dir.glob("paper_decisions_*.jsonl"))
            self.assertEqual(len(decisions), 1)
            decision_rows = [json.loads(line) for line in decisions[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(decision_rows[0]["decision"], "reject")
            self.assertIn("top_book_imbalance=ask_heavy", decision_rows[0]["reason"])

    def test_run_once_processes_oldest_unprocessed_paper_candidates_not_tail_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state_path = data_dir / ".paper_decision_state.json"
            btc_base = _ms("2026-08-04T00:00:00+00:00")
            btc_candles = [_bar(btc_base + i * 60_000, 100 + i * 0.2) for i in range(40)]
            _write_jsonl(data_dir / "ws_candle" / "btc_2026-08-04.jsonl", [{"payload": c} for c in btc_candles])

            cascades = []
            for i in range(8):
                cascades.append(
                    {
                        "symbol": "BTC" if i in {0, 1, 2} else "SOL",
                        "side": "B",
                        "start_ts": f"2026-08-04T00:{20 + i:02d}:30+00:00",
                        "event_vwap": 104.0,
                        "n_fills": 12 + i,
                        "total_notional": 500000 + i,
                        "top_book_imbalance": 0.1,
                    }
                )
            _write_jsonl(data_dir / "cascades.jsonl", cascades)

            first = run_once(data_dir=data_dir, state_path=state_path, max_new=1)
            second = run_once(data_dir=data_dir, state_path=state_path, max_new=1)

            self.assertEqual(first["decisions_written"], 1)
            self.assertEqual(second["decisions_written"], 1)
            decisions = list(data_dir.glob("paper_decisions_*.jsonl"))
            rows = [json.loads(line) for line in decisions[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertIn("00:20:30", rows[0]["cascade_key"])
            self.assertIn("00:21:30", rows[1]["cascade_key"])


if __name__ == "__main__":
    unittest.main()

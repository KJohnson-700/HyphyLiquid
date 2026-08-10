"""Tests for the live-like paper decision loop."""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.paper_decision_loop import (  # noqa: E402
    ETH_FLOW_WINDOW_SECONDS,
    ETH_MAX_HOLD_MINUTES,
    ETH_REQUIRED_IMBALANCE_BUCKET,
    ETH_REQUIRED_FLOW_LABEL,
    ETH_REQUIRED_FUNDING_BUCKET,
    ETH_STOP_BUFFER_BPS,
    PAPER_SYMBOLS,
    _trade_flow_label,
    _trade_flow_stats,
    _build_eth_position,
    build_position_for_cascade,
    run_once,
)
from src.execution.paper_broker import PaperBracket, PaperPosition, mark_position  # noqa: E402


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _bar(t_ms: int, c: float, h: float | None = None, l: float | None = None, *, s: str = "BTC") -> dict:
    return {
        "t": t_ms,
        "T": t_ms + 59_999,
        "o": str(c),
        "h": str(h if h is not None else c + 0.01),
        "l": str(l if l is not None else c - 0.01),
        "c": str(c),
        "s": s,
        "i": "1m",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _trade(t_ms: int, side: str, px: float = 100.0, sz: float = 1.0) -> dict:
    return {"ts": t_ms, "side": side, "px": px, "sz": sz}


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

    def test_run_once_opens_and_marks_btc_eth_and_hype(self):
        # Per Slim 2026-08-06: ETH joined the paper loop as a research-only
        # lane. The synthetic ETH cascade below has no top_book_imbalance,
        # so the ETH gate rejects (ask_heavy required). It still records a
        # decision in paper_decisions so the lane's behaviour is auditable.
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

            eth_base = _ms("2026-08-04T00:00:00+00:00")
            eth_candles = [_bar(eth_base + i * 60_000, 100 + i * 0.2) for i in range(25)]
            _write_jsonl(data_dir / "ws_candle" / "eth_2026-08-04.jsonl", [{"payload": c} for c in eth_candles])

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
                    # No top_book_imbalance: gate rejects
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
            # ETH now in PAPER_SYMBOLS -- its cascade gets a decision row,
            # but B-side book-persistence is retired from new paper opens.
            self.assertEqual({row["symbol"] for row in decision_rows}, {"BTC", "ETH", "HYPE"})
            self.assertIn("v1_paper", {row["paper_scope"] for row in decision_rows})
            self.assertIn("research_paper", {row["paper_scope"] for row in decision_rows})
            eth_decision = next(r for r in decision_rows if r["symbol"] == "ETH")
            self.assertEqual(eth_decision["decision"], "reject")
            self.assertIn("retired", eth_decision["reason"])

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


class TestEthPaperLane(unittest.TestCase):
    """ETH funding-context follow lane.

    Per Slim 2026-08-09: narrow the project to the most viable v1 candidate
    ASAP. The active ETH candidate is side=A follow at 60m when funding Z is
    positive/elevated; the older ETH B-side book-persistence paper lane is
    retired from new opens after negative forward paper.
    """

    def test_eth_is_in_paper_symbols(self):
        self.assertIn("ETH", PAPER_SYMBOLS)

    def test_eth_constants_are_documented(self):
        # The active ETH lane is calibrated against:
        # ETH side=A follow 60m funding_z=funding_pos_elevated
        # PF 1.5661 n=58 median +0.0760% from the context backtest.
        self.assertEqual(ETH_REQUIRED_IMBALANCE_BUCKET, "ask_heavy")
        self.assertEqual(ETH_REQUIRED_FLOW_LABEL, "amplifies")
        self.assertEqual(ETH_FLOW_WINDOW_SECONDS, 30)
        self.assertEqual(ETH_REQUIRED_FUNDING_BUCKET, "funding_pos_elevated")
        self.assertEqual(ETH_STOP_BUFFER_BPS, 35.0)
        self.assertEqual(ETH_MAX_HOLD_MINUTES, 60)

    def _funding_ctx_rows(self, base_ms: int) -> list[dict]:
        rows = []
        for i in range(35):
            rows.append({
                "ts": base_ms - (35 - i) * 60_000,
                "funding": 0.000009 if i % 2 == 0 else 0.000011,
                "oi": 800_000 + i,
                "mark": 100 + i * 0.01,
            })
        rows.append({
            "ts": base_ms,
            "funding": 0.0000115,
            "oi": 800_100,
            "mark": 100.0,
        })
        return rows

    def test_eth_a_side_funding_follow_opens_v1_paper_position(self):
        # Synthesize: ETH A-side cascade at 100, no reclaim in 1m wait,
        # and funding Z is positive/elevated. Follow direction for side A
        # is SHORT, with a 60m time horizon.
        eth_base = _ms("2026-08-04T00:00:00+00:00")
        eth_candles = [_bar(eth_base + i * 60_000, 99.8 - 0.01 * i, s="ETH") for i in range(80)]
        cascade = {
            "symbol": "ETH",
            "side": "A",
            "start_ts": "2026-08-04T00:00:30+00:00",  # 30s into the hour
            "event_vwap": 100.0,
            "n_fills": 5,
            "total_notional": 75000,
        }

        decision, position = build_position_for_cascade(
            cascade,
            {"ETH": eth_candles},
            {"ETH": []},
            {"ETH": self._funding_ctx_rows(eth_base)},
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "open_position")
        self.assertEqual(decision.lane, "eth_funding_context_follow")
        self.assertEqual(decision.paper_scope, "v1_paper")
        self.assertTrue(decision.execution_allowed)
        self.assertIsNotNone(position)
        self.assertEqual(position.symbol, "ETH")
        self.assertEqual(position.side, "A")
        self.assertEqual(position.direction, "short")
        self.assertEqual(position.lane, "eth_funding_context_follow")
        self.assertEqual(position.paper_scope, "v1_paper")
        expected_stop = 100.0 * (1.0 + ETH_STOP_BUFFER_BPS / 10_000.0)
        self.assertAlmostEqual(position.bracket.initial_stop_price, expected_stop, places=6)
        self.assertEqual(position.bracket.max_hold_minutes, ETH_MAX_HOLD_MINUTES)
        self.assertIsNone(position.bracket.trail_bps)
        self.assertIsNone(position.bracket.target_price)
        self.assertIn("paper_gate", position.metadata)
        self.assertIn(ETH_REQUIRED_FUNDING_BUCKET, position.metadata["paper_gate"])
        self.assertEqual(position.metadata["funding_z_bucket"], ETH_REQUIRED_FUNDING_BUCKET)
        self.assertIn("research_source", position.metadata)

    def test_eth_rejects_without_funding_context(self):
        eth_base = _ms("2026-08-04T00:00:00+00:00")
        eth_candles = [_bar(eth_base + i * 60_000, 99.8 - 0.01 * i, s="ETH") for i in range(80)]
        cascade = {
            "symbol": "ETH",
            "side": "A",
            "start_ts": "2026-08-04T00:00:30+00:00",
            "event_vwap": 100.0,
            "n_fills": 5,
            "total_notional": 75000,
        }

        decision, position = build_position_for_cascade(cascade, {"ETH": eth_candles}, {"ETH": []}, {"ETH": []})

        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "reject")
        self.assertIn("funding_z=funding_pos_elevated", decision.reason)
        self.assertIsNone(position)

    def test_eth_b_side_book_persistence_is_retired(self):
        eth_base = _ms("2026-08-04T00:00:00+00:00")
        eth_candles = [_bar(eth_base + i * 60_000, 100.0 + 0.1 * i, s="ETH") for i in range(20)]
        cascade = {
            "symbol": "ETH",
            "side": "B",
            "start_ts": "2026-08-04T00:00:30+00:00",
            "event_vwap": 100.0,
            "n_fills": 5,
            "total_notional": 75000,
            "top_book_imbalance": -0.5,
        }

        decision, position = build_position_for_cascade(cascade, {"ETH": eth_candles}, {"ETH": []})

        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "reject")
        self.assertIn("retired", decision.reason)
        self.assertIsNone(position)

    def test_eth_rejects_when_reclaim_happens(self):
        # A-side follow is invalidated if the next completed bar closes
        # back above event_vwap.
        eth_base = _ms("2026-08-04T00:00:00+00:00")
        eth_candles = [
            _bar(eth_base, 100.5, s="ETH"),
            _bar(eth_base + 60_000, 100.2, s="ETH"),  # reclaim for side A
        ] + [_bar(eth_base + (i + 2) * 60_000, 99.9, s="ETH") for i in range(80)]
        cascade = {
            "symbol": "ETH",
            "side": "A",
            "start_ts": "2026-08-04T00:00:30+00:00",
            "event_vwap": 100.0,
            "n_fills": 5,
            "total_notional": 75000,
        }

        decision, position = build_position_for_cascade(
            cascade,
            {"ETH": eth_candles},
            {"ETH": []},
            {"ETH": self._funding_ctx_rows(eth_base)},
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, "reject")
        self.assertTrue(
            "reclaim" in decision.reason.lower(),
            f"expected reclaim-related reason, got: {decision.reason!r}",
        )
        self.assertIsNone(position)

    def test_eth_bracket_sizing_uses_risk_module(self):
        eth_base = _ms("2026-08-04T00:00:00+00:00")
        eth_candles = [_bar(eth_base + i * 60_000, 99.8 - 0.01 * i, s="ETH") for i in range(80)]
        cascade = {
            "symbol": "ETH",
            "side": "A",
            "start_ts": "2026-08-04T00:00:30+00:00",
            "event_vwap": 100.0,
            "n_fills": 5,
            "total_notional": 75000,
        }

        _, position = build_position_for_cascade(
            cascade,
            {"ETH": eth_candles},
            {"ETH": []},
            {"ETH": self._funding_ctx_rows(eth_base)},
        )

        self.assertIsNotNone(position)
        self.assertEqual(position.direction, "short")
        self.assertGreater(position.notional_usd, 1700.0)
        self.assertLess(position.notional_usd, 2200.0)
        self.assertAlmostEqual(position.risk_usd, 10.0, places=1)

    def test_eth_trade_flow_label_matches_backtest_semantics(self):
        stats = _trade_flow_stats([
            _trade(1000, "A"),
            _trade(1001, "A"),
            _trade(1002, "A"),
            _trade(1003, "B"),
        ])

        self.assertLess(stats["flow_imbalance"], 0)
        self.assertEqual(_trade_flow_label("B", stats["flow_imbalance"]), "amplifies")


if __name__ == "__main__":
    unittest.main()

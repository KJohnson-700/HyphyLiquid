"""Tests for paper-to-execution intent conversion."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.execution.paper_broker import PaperBracket, PaperPosition  # noqa: E402
from src.execution.paper_intents import (  # noqa: E402
    ACTIVE_EXECUTION_LANE,
    build_latest_eth_intent_preview,
    latest_active_eth_position,
    paper_position_to_bracket_intent,
)


def _eth_position(**overrides) -> PaperPosition:
    data = {
        "paper_id": "paper-eth",
        "paper_scope": "v1_paper",
        "cascade_key": "ETH|A|2026-08-10T00:00:00+00:00|9|100000",
        "symbol": "ETH",
        "side": "A",
        "lane": ACTIVE_EXECUTION_LANE,
        "direction": "short",
        "event_ts": "2026-08-10T00:00:00+00:00",
        "entry_ts": "2026-08-10T00:01:00+00:00",
        "entry_idx": 1,
        "entry_price": 3000.0,
        "notional_usd": 2857.1429,
        "risk_usd": 10.0,
        "bracket": PaperBracket(
            entry_price=3000.0,
            initial_stop_price=3010.5,
            target_price=None,
            activation_price=None,
            trail_bps=None,
            max_hold_minutes=60,
            stop_slippage_bps=2.0,
            round_trip_cost_bps=8.0,
        ),
        "metadata": {"paper_gate": "funding_z=funding_pos_elevated"},
    }
    data.update(overrides)
    return PaperPosition(**data)


class TestPaperIntents(unittest.TestCase):
    def test_eth_position_converts_to_stop_only_bracket_intent(self):
        intent = paper_position_to_bracket_intent(_eth_position())

        self.assertEqual(intent.symbol, "ETH")
        self.assertEqual(intent.side, "short")
        self.assertEqual(intent.entry_px, 3000.0)
        self.assertEqual(intent.sl_px, 3010.5)
        self.assertIsNone(intent.tp_px)
        self.assertEqual(intent.notional_usd, 2857.1429)
        self.assertIn("timeout_exit=bot_managed", intent.reason)

    def test_rejects_retired_or_research_lanes(self):
        with self.assertRaisesRegex(ValueError, "active execution lane"):
            paper_position_to_bracket_intent(_eth_position(lane="eth_book_persistence_fade"))
        with self.assertRaisesRegex(ValueError, "v1_paper"):
            paper_position_to_bracket_intent(_eth_position(paper_scope="research_paper"))
        with self.assertRaisesRegex(ValueError, "ETH short"):
            paper_position_to_bracket_intent(_eth_position(direction="long"))

    def test_latest_preview_reads_append_only_position_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = data_dir / "paper_positions_20260810.jsonl"
            position = _eth_position()
            path.write_text(json.dumps({"event": "opened", **position.to_dict()}) + "\n", encoding="utf-8")

            latest = latest_active_eth_position(data_dir)
            preview = build_latest_eth_intent_preview(data_dir)

            self.assertIsNotNone(latest)
            self.assertTrue(preview["eligible"])
            self.assertEqual(preview["paper_id"], "paper-eth")
            self.assertEqual(preview["timeout_exit"], "bot_managed")

    def test_preview_reports_no_position_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            preview = build_latest_eth_intent_preview(Path(tmp))

            self.assertFalse(preview["eligible"])
            self.assertIn("no ETH", preview["reason"])


if __name__ == "__main__":
    unittest.main()

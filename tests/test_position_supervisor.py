"""Tests for the ETH position lifecycle supervisor."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.execution.paper_broker import PaperBracket, PaperPosition  # noqa: E402
from src.execution.paper_intents import ACTIVE_EXECUTION_LANE  # noqa: E402
from src.execution.position_supervisor import (  # noqa: E402
    ManagedPosition,
    build_latest_eth_timeout_preview,
    build_timeout_decision,
    execute_reduce_only_close,
    latest_open_eth_managed_position,
)


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _managed(**overrides) -> ManagedPosition:
    data = {
        "position_id": "paper-eth",
        "symbol": "ETH",
        "side": "short",
        "size_coin": 1.25,
        "entry_ts": _ts("2026-08-11T00:00:00+00:00"),
        "max_hold_minutes": 60,
        "source": "paper:eth_funding_context_follow",
    }
    data.update(overrides)
    return ManagedPosition(**data)


def _paper_position(**overrides) -> PaperPosition:
    data = {
        "paper_id": "paper-eth",
        "paper_scope": "v1_paper",
        "cascade_key": "ETH|A|2026-08-11T00:00:00+00:00|9|100000",
        "symbol": "ETH",
        "side": "A",
        "lane": ACTIVE_EXECUTION_LANE,
        "direction": "short",
        "event_ts": "2026-08-11T00:00:00+00:00",
        "entry_ts": "2026-08-11T00:01:00+00:00",
        "entry_idx": 1,
        "entry_price": 3000.0,
        "notional_usd": 3000.0,
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
        "metadata": {},
    }
    data.update(overrides)
    return PaperPosition(**data)


class FakeExchange:
    def __init__(self):
        self.orders = []

    def order(self, *args, **kwargs):
        self.orders.append((args, kwargs))
        return {"ok": True}


class FakeMarketCloseExchange:
    def __init__(self):
        self.calls = []

    def market_close(self, symbol, sz):
        self.calls.append((symbol, sz))
        return {"closed": True}


class TestPositionSupervisor(unittest.TestCase):
    def test_timeout_holds_before_due_time(self):
        decision = build_timeout_decision(
            _managed(),
            now=_ts("2026-08-11T00:59:59+00:00"),
        )

        self.assertEqual(decision.action, "hold")
        self.assertIsNone(decision.close_intent)

    def test_timeout_builds_reduce_only_buy_for_short(self):
        decision = build_timeout_decision(
            _managed(),
            now=_ts("2026-08-11T01:00:00+00:00"),
        )

        self.assertEqual(decision.action, "close")
        self.assertIsNotNone(decision.close_intent)
        self.assertTrue(decision.close_intent.is_buy)
        self.assertTrue(decision.close_intent.reduce_only)

    def test_rejects_non_v1_symbols(self):
        decision = build_timeout_decision(_managed(symbol="HYPE"))

        self.assertEqual(decision.action, "reject")
        self.assertIn("v1", decision.reason)

    def test_execute_reduce_only_close_uses_market_close_when_available(self):
        exchange = FakeMarketCloseExchange()
        decision = build_timeout_decision(_managed(), now=_ts("2026-08-11T02:00:00+00:00"))

        result = execute_reduce_only_close(exchange, decision.close_intent)

        self.assertTrue(result.submitted)
        self.assertEqual(exchange.calls, [("ETH", 1.25)])

    def test_execute_reduce_only_close_falls_back_to_ioc_reduce_only_order(self):
        exchange = FakeExchange()
        decision = build_timeout_decision(_managed(), now=_ts("2026-08-11T02:00:00+00:00"))

        result = execute_reduce_only_close(exchange, decision.close_intent, mark_px=3000.0, slippage_bps=10)

        self.assertTrue(result.submitted)
        args, kwargs = exchange.orders[0]
        self.assertEqual(args[0], "ETH")
        self.assertTrue(args[1])
        self.assertEqual(args[2], 1.25)
        self.assertEqual(args[4], {"limit": {"tif": "Ioc"}})
        self.assertTrue(kwargs["reduce_only"])

    def test_latest_open_eth_ignores_closed_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            path = data_dir / "paper_positions_20260811.jsonl"
            closed = _paper_position(paper_id="closed-eth")
            open_ = _paper_position(paper_id="open-eth", entry_price=2500.0, notional_usd=5000.0)
            rows = [
                {"event": "opened", **closed.to_dict()},
                {"event": "mark", "paper_id": "closed-eth", "fill": {"status": "closed"}},
                {"event": "opened", **open_.to_dict()},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            managed = latest_open_eth_managed_position(data_dir)
            preview = build_latest_eth_timeout_preview(data_dir, now=_ts("2026-08-11T02:02:00+00:00"))

            self.assertIsNotNone(managed)
            self.assertEqual(managed.position_id, "open-eth")
            self.assertEqual(managed.size_coin, 2.0)
            self.assertEqual(preview["action"], "close")
            self.assertEqual(preview["position_id"], "open-eth")

    def test_latest_preview_reports_no_open_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            preview = build_latest_eth_timeout_preview(Path(tmp), now=datetime.now(timezone.utc))

            self.assertFalse(preview["eligible"])
            self.assertIn("no open ETH", preview["reason"])


if __name__ == "__main__":
    unittest.main()

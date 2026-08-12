"""Tests for local-vs-Hyperliquid reconciliation."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.execution.position_supervisor import ManagedPosition  # noqa: E402
from src.execution.reconciler import (  # noqa: E402
    build_exchange_snapshot,
    has_protective_stop,
    reconcile,
)


def _local(**overrides) -> ManagedPosition:
    data = {
        "position_id": "paper-eth",
        "symbol": "ETH",
        "side": "short",
        "size_coin": 1.25,
        "entry_ts": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "max_hold_minutes": 60,
        "source": "paper:eth_funding_context_follow",
    }
    data.update(overrides)
    return ManagedPosition(**data)


def _snapshot(*, szi="-1.25", orders=None, symbol="ETH"):
    user_state = {
        "marginSummary": {"accountValue": "1000.5"},
        "assetPositions": [
            {
                "position": {
                    "coin": symbol,
                    "szi": szi,
                    "entryPx": "1870.0",
                    "positionValue": "2337.5",
                }
            }
        ],
    }
    if orders is None:
        orders = [
            {
                "coin": symbol,
                "side": "B",
                "sz": "1.25",
                "oid": 123,
                "reduceOnly": True,
                "isTrigger": True,
                "isPositionTpsl": True,
                "orderType": "Stop Market",
                "triggerPx": "1880.0",
            }
        ]
    return build_exchange_snapshot(user_state, orders, user="0xabc", captured_at=datetime(2026, 8, 11, tzinfo=timezone.utc))


class TestReconciler(unittest.TestCase):
    def test_parses_snapshot_positions_and_orders(self):
        snapshot = _snapshot()

        self.assertEqual(snapshot.account_value, 1000.5)
        self.assertEqual(snapshot.positions[0].symbol, "ETH")
        self.assertEqual(snapshot.positions[0].side, "short")
        self.assertEqual(snapshot.positions[0].size_coin, 1.25)
        self.assertEqual(snapshot.orders[0].side, "buy")

    def test_matching_local_exchange_with_stop_is_ok(self):
        report = reconcile(local_position=_local(), exchange_snapshot=_snapshot())

        self.assertEqual(report.status, "ok")
        self.assertFalse(report.blocking)
        self.assertEqual(report.action, "safe_to_supervise")

    def test_missing_exchange_snapshot_skips_live_reconciliation(self):
        report = reconcile(local_position=_local(), exchange_snapshot=None)

        self.assertEqual(report.status, "skipped")
        self.assertFalse(report.blocking)
        self.assertEqual(report.action, "do_not_live_trade")

    def test_exchange_position_without_local_state_blocks(self):
        report = reconcile(local_position=None, exchange_snapshot=_snapshot())

        self.assertTrue(report.blocking)
        self.assertIn("exchange_position_without_local_state", {f.code for f in report.findings})

    def test_local_position_missing_on_exchange_blocks(self):
        empty = build_exchange_snapshot(
            {"marginSummary": {"accountValue": "1000"}, "assetPositions": []},
            [],
            user="0xabc",
        )

        report = reconcile(local_position=_local(), exchange_snapshot=empty)

        self.assertTrue(report.blocking)
        self.assertIn("local_position_missing_on_exchange", {f.code for f in report.findings})

    def test_missing_protective_stop_blocks(self):
        report = reconcile(local_position=_local(), exchange_snapshot=_snapshot(orders=[]))

        self.assertTrue(report.blocking)
        self.assertIn("protective_stop_missing", {f.code for f in report.findings})

    def test_side_and_size_mismatch_block(self):
        report = reconcile(local_position=_local(side="long", size_coin=0.5), exchange_snapshot=_snapshot())
        codes = {f.code for f in report.findings}

        self.assertTrue(report.blocking)
        self.assertIn("side_mismatch", codes)
        self.assertIn("size_mismatch", codes)

    def test_non_v1_position_blocks(self):
        report = reconcile(local_position=None, exchange_snapshot=_snapshot(symbol="HYPE"))

        self.assertTrue(report.blocking)
        self.assertIn("unexpected_non_v1_position", {f.code for f in report.findings})

    def test_has_protective_stop_requires_reduce_only_trigger_close_side(self):
        snapshot = _snapshot()

        self.assertTrue(has_protective_stop(snapshot.positions[0], snapshot.orders))

        bad_orders = build_exchange_snapshot(
            {"assetPositions": [{"position": {"coin": "ETH", "szi": "-1.25"}}]},
            [{"coin": "ETH", "side": "A", "sz": "1.25", "reduceOnly": True, "isTrigger": True}],
            user="0xabc",
        )
        self.assertFalse(has_protective_stop(bad_orders.positions[0], bad_orders.orders))


if __name__ == "__main__":
    unittest.main()

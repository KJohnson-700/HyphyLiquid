"""Tests for the guarded testnet proof runner logic."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.execution.testnet_proof import (  # noqa: E402
    build_protective_stop_order,
    build_ioc_order,
    check_testnet_proof_guard,
    run_fetch_only_proof,
    run_bracket_proof,
    run_order_proof,
)


class FakeInfo:
    def __init__(self, positions=None, orders=None, mids=None):
        self.positions = positions or []
        self.orders = orders or []
        self.mids = mids or {"ETH": "3000.0"}

    def user_state(self, _user):
        return {"marginSummary": {"accountValue": "1000"}, "assetPositions": self.positions}

    def frontend_open_orders(self, _user):
        return self.orders

    def all_mids(self):
        return self.mids


class StatefulInfo(FakeInfo):
    def __init__(self):
        super().__init__(positions=[], orders=[], mids={"ETH": "3000.0"})
        self.calls = 0

    def user_state(self, _user):
        self.calls += 1
        if self.calls == 1:
            positions = []
        elif self.calls == 2:
            positions = [{"position": {"coin": "ETH", "szi": "-0.01", "entryPx": "3000", "positionValue": "30"}}]
        else:
            positions = []
        return {"marginSummary": {"accountValue": "1000"}, "assetPositions": positions}


class FakeExchange:
    def __init__(self):
        self.orders = []

    def order(self, *args, **kwargs):
        self.orders.append((args, kwargs))
        return {"status": "ok"}

    def market_close(self, symbol, sz):
        self.orders.append(("market_close", symbol, sz))
        return {"status": "ok"}


class FakeBracketExchange(FakeExchange):
    def __init__(self):
        super().__init__()
        self.cancelled = []

    def bulk_orders(self, requests, grouping=None):
        self.orders.append(("bulk_orders", requests, grouping))
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1}}, {"resting": {"oid": 2}}]}}}

    def bulk_cancel(self, requests):
        self.cancelled.append(requests)
        return {"status": "ok"}


class StatefulBracketInfo(FakeInfo):
    def __init__(self, include_stop=True):
        super().__init__(positions=[], orders=[], mids={"ETH": "3000.0"})
        self.calls = 0
        self.include_stop = include_stop

    def user_state(self, _user):
        self.calls += 1
        if self.calls == 1:
            positions = []
        elif self.calls in {2, 3}:
            positions = [{"position": {"coin": "ETH", "szi": "-0.01", "entryPx": "3000", "positionValue": "30"}}]
        else:
            positions = []
        return {"marginSummary": {"accountValue": "1000"}, "assetPositions": positions}

    def frontend_open_orders(self, _user):
        if self.calls == 2 and self.include_stop:
            return [
                {
                    "coin": "ETH",
                    "side": "B",
                    "sz": "0.01",
                    "oid": 2,
                    "reduceOnly": True,
                    "isTrigger": True,
                    "isPositionTpsl": True,
                    "orderType": "Stop Market",
                    "triggerPx": "3010.5",
                }
            ]
        if self.calls == 3 and self.include_stop:
            return [
                {
                    "coin": "ETH",
                    "side": "B",
                    "sz": "0.01",
                    "oid": 2,
                    "reduceOnly": True,
                    "isTrigger": True,
                    "isPositionTpsl": True,
                    "orderType": "Stop Market",
                    "triggerPx": "3010.5",
                }
            ]
        return []


class TestTestnetProof(unittest.TestCase):
    def test_guard_allows_dry_run_without_user(self):
        guard = check_testnet_proof_guard(env="mainnet")

        self.assertTrue(guard.allowed)
        self.assertIn("dry-run", guard.reason)

    def test_guard_refuses_mainnet_fetch_or_execute(self):
        guard = check_testnet_proof_guard(env="mainnet", user="0xabc", fetch_exchange=True)

        self.assertFalse(guard.allowed)
        self.assertIn("testnet", guard.reason)

    def test_guard_requires_explicit_order_arm(self):
        guard = check_testnet_proof_guard(env="testnet", user="0xabc", fetch_exchange=True, execute_orders=True)

        self.assertFalse(guard.allowed)
        self.assertIn("i-understand", guard.reason)

    def test_build_ioc_order_short_is_sell_aggressive_below_mid(self):
        order = build_ioc_order(symbol="ETH", side="short", size_coin=0.01, mark_px=3000.0, reduce_only=False, slippage_bps=10)

        self.assertEqual(order["name"], "ETH")
        self.assertFalse(order["is_buy"])
        self.assertEqual(order["limit_px"], 2997.0)
        self.assertEqual(order["order_type"], {"limit": {"tif": "Ioc"}})

    def test_build_ioc_order_rounds_eth_to_tick(self):
        order = build_ioc_order(symbol="ETH", side="short", size_coin=0.01, mark_px=1886.7, reduce_only=False, slippage_bps=20)

        self.assertEqual(order["limit_px"], 1882.9)

    def test_build_ioc_order_rounds_btc_to_integer_tick(self):
        order = build_ioc_order(symbol="BTC", side="long", size_coin=0.001, mark_px=119432.4, reduce_only=False, slippage_bps=20)

        self.assertEqual(order["limit_px"], 119671.0)

    def test_build_protective_stop_order_short_closes_buy_above_mark(self):
        order = build_protective_stop_order(symbol="ETH", side="short", size_coin=0.01, mark_px=3000.0, stop_bps=35)

        self.assertEqual(order["name"], "ETH")
        self.assertTrue(order["is_buy"])
        self.assertTrue(order["reduce_only"])
        self.assertEqual(order["limit_px"], 3010.5)
        self.assertEqual(order["order_type"], {"trigger": {"triggerPx": 3010.5, "isMarket": True, "tpsl": "sl"}})

    def test_build_protective_stop_order_long_closes_sell_below_mark(self):
        order = build_protective_stop_order(symbol="ETH", side="long", size_coin=0.01, mark_px=3000.0, stop_bps=35)

        self.assertFalse(order["is_buy"])
        self.assertEqual(order["limit_px"], 2989.5)

    def test_fetch_only_proof_reconciles_flat_account(self):
        result = run_fetch_only_proof(FakeInfo(), user="0xabc")

        self.assertEqual(result["mode"], "fetch_exchange")
        self.assertFalse(result["reconciliation"]["blocking"])
        self.assertEqual(result["orders_sent"], [])

    def test_order_proof_opens_then_reduce_only_closes(self):
        info = StatefulInfo()
        exchange = FakeExchange()

        result = run_order_proof(info, exchange, user="0xabc", size_coin=0.01)

        self.assertEqual(result["status"], "close_submitted")
        self.assertEqual(len(result["orders_sent"]), 2)
        self.assertEqual(len(exchange.orders), 2)
        self.assertEqual(exchange.orders[1], ("market_close", "ETH", 0.01))

    def test_bracket_proof_verifies_protective_stop_then_cleans_up(self):
        info = StatefulBracketInfo(include_stop=True)
        exchange = FakeBracketExchange()

        result = run_bracket_proof(info, exchange, user="0xabc", size_coin=0.01)

        self.assertEqual(result["status"], "bracket_proof_passed")
        self.assertTrue(result["protective_stop_visible"])
        self.assertEqual(len(result["orders_sent"]), 2)
        self.assertEqual(exchange.orders[0][0], "bulk_orders")
        self.assertEqual(exchange.orders[0][1][0]["coin"], "ETH")
        self.assertNotIn("name", exchange.orders[0][1][0])
        self.assertEqual(exchange.orders[0][2], "normalTpsl")
        self.assertEqual(exchange.orders[1], ("market_close", "ETH", 0.01))
        self.assertEqual(exchange.cancelled, [[{"coin": "ETH", "oid": 2}]])

    def test_bracket_proof_reports_missing_protective_stop(self):
        info = StatefulBracketInfo(include_stop=False)
        exchange = FakeBracketExchange()

        result = run_bracket_proof(info, exchange, user="0xabc", size_coin=0.01)

        self.assertEqual(result["status"], "protective_stop_missing")
        self.assertFalse(result["protective_stop_visible"])

    def test_order_proof_blocks_if_existing_position_present(self):
        info = FakeInfo(positions=[{"position": {"coin": "ETH", "szi": "-0.01"}}])
        exchange = FakeExchange()

        result = run_order_proof(info, exchange, user="0xabc", size_coin=0.01)

        self.assertEqual(result["status"], "blocked_before_open")
        self.assertEqual(exchange.orders, [])


if __name__ == "__main__":
    unittest.main()

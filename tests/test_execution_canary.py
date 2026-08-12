"""Tests for the execution canary live guard."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_execution_canary import LIVE_ARM_ENV, build_status, check_live_guard  # noqa: E402


class TestExecutionCanaryGuard(unittest.TestCase):
    def test_live_guard_refuses_testnet(self):
        guard = check_live_guard(cli_armed=True, env="testnet", env_armed="1")

        self.assertFalse(guard.allowed)
        self.assertIn("mainnet", guard.reason)

    def test_live_guard_requires_env_arm(self):
        guard = check_live_guard(cli_armed=True, env="mainnet", env_armed="")

        self.assertFalse(guard.allowed)
        self.assertIn(LIVE_ARM_ENV, guard.reason)

    def test_live_guard_requires_cli_arm(self):
        guard = check_live_guard(cli_armed=False, env="mainnet", env_armed="1")

        self.assertFalse(guard.allowed)
        self.assertIn("real orders", guard.reason)

    def test_live_guard_allows_only_double_armed_mainnet(self):
        guard = check_live_guard(cli_armed=True, env="mainnet", env_armed="1")

        self.assertTrue(guard.allowed)
        self.assertEqual(guard.reason, "live mode armed")

    def test_status_blocks_live_when_guard_refuses(self):
        guard = check_live_guard(cli_armed=False, env="mainnet", env_armed="1")

        status = build_status(mode="live", guard=guard)

        self.assertFalse(status["live_guard"]["allowed"])
        self.assertIn("refused", status["next_action"])

    def test_status_carries_eth_intent_preview(self):
        guard = check_live_guard(cli_armed=False, env="testnet", env_armed="")

        status = build_status(
            mode="paper",
            guard=guard,
            paper_payload={
                "paper_pass": {"decisions_written": 1},
                "eth_intent_preview": {"eligible": True, "lane": "eth_funding_context_follow"},
                "timeout_supervisor_preview": {"eligible": True, "action": "hold"},
                "reconciliation": {"status": "skipped", "blocking": False},
            },
        )

        self.assertTrue(status["eth_intent_preview"]["eligible"])
        self.assertEqual(status["eth_intent_preview"]["lane"], "eth_funding_context_follow")
        self.assertEqual(status["timeout_supervisor_preview"]["action"], "hold")
        self.assertFalse(status["reconciliation"]["blocking"])


if __name__ == "__main__":
    unittest.main()

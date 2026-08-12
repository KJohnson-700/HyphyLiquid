"""Tests for the execution canary live guard."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_execution_canary import LIVE_ARM_ENV, build_execution_proof_gate, build_status, check_live_guard  # noqa: E402


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
                "execution_proof_gate": {"passed": True, "reason": "ok"},
            },
        )

        self.assertTrue(status["eth_intent_preview"]["eligible"])
        self.assertEqual(status["eth_intent_preview"]["lane"], "eth_funding_context_follow")
        self.assertEqual(status["timeout_supervisor_preview"]["action"], "hold")
        self.assertFalse(status["reconciliation"]["blocking"])

    def test_execution_proof_gate_passes_latest_bracket_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "testnet_proof_status.json"
            path.write_text(
                """
                {
                  "generated_at": "2026-08-12T07:07:31+00:00",
                  "mode": "execute_testnet_bracket",
                  "status": "bracket_proof_passed",
                  "protective_stop_visible": true,
                  "after_cleanup": {"positions": [], "orders": []}
                }
                """,
                encoding="utf-8",
            )

            gate = build_execution_proof_gate(Path(tmp))

            self.assertTrue(gate["passed"])
            self.assertEqual(gate["after_cleanup_positions"], 0)

    def test_execution_proof_gate_blocks_missing_or_weak_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = build_execution_proof_gate(Path(tmp))
            path = Path(tmp) / "testnet_proof_status.json"
            path.write_text(
                """
                {
                  "mode": "execute_testnet_orders",
                  "status": "close_submitted",
                  "protective_stop_visible": false,
                  "after_close": {"positions": [], "orders": []}
                }
                """,
                encoding="utf-8",
            )
            weak = build_execution_proof_gate(Path(tmp))

            self.assertFalse(missing["passed"])
            self.assertIn("missing", missing["reason"])
            self.assertFalse(weak["passed"])
            self.assertIn("mode_is_bracket", weak["reason"])

    def test_status_blocks_when_execution_proof_gate_fails(self):
        guard = check_live_guard(cli_armed=True, env="mainnet", env_armed="1")

        status = build_status(
            mode="paper",
            guard=guard,
            paper_payload={
                "audit": {"anomalies": 0},
                "reconciliation": {"blocking": False},
                "execution_proof_gate": {"passed": False, "reason": "proof stale"},
            },
        )

        self.assertIn("Execution proof gate is blocking", status["next_action"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the live-like readiness rollup."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_live_like_readiness import evaluate_readiness  # noqa: E402


def _canary(**overrides):
    data = {
        "generated_at": "2026-08-12T07:45:15+00:00",
        "paper_pass": {"processed_total": 10},
        "audit": {"anomalies": 0, "open_now": 0, "decisions": 10, "opened": 1, "closed": 1},
        "execution_proof_gate": {
            "passed": True,
            "proof_status": "bracket_proof_passed",
            "protective_stop_visible": True,
        },
        "reconciliation": {"status": "ok", "blocking": False},
        "eth_intent_preview": {"eligible": True},
        "timeout_supervisor_preview": {"eligible": True, "action": "hold"},
        "next_action": "operator review",
    }
    data.update(overrides)
    return data


class TestLiveLikeReadiness(unittest.TestCase):
    def test_pass_when_all_checks_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "execution_canary_status.json").write_text(json.dumps(_canary()), encoding="utf-8")

            status = evaluate_readiness(Path(tmp))

            self.assertEqual(status["verdict"], "PASS")
            self.assertIn("operator review", status["next_action"])

    def test_hold_for_open_paper_positions_or_skipped_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "execution_canary_status.json").write_text(
                json.dumps(_canary(audit={"anomalies": 0, "open_now": 2}, reconciliation={"status": "skipped", "blocking": False})),
                encoding="utf-8",
            )

            status = evaluate_readiness(Path(tmp))

            self.assertEqual(status["verdict"], "HOLD")
            self.assertIn("paper positions still open", status["next_action"])

    def test_block_when_proof_gate_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "execution_canary_status.json").write_text(
                json.dumps(_canary(execution_proof_gate={"passed": False, "reason": "proof missing"})),
                encoding="utf-8",
            )

            status = evaluate_readiness(Path(tmp))

            self.assertEqual(status["verdict"], "BLOCK")
            self.assertIn("execution_proof_gate", status["next_action"])


if __name__ == "__main__":
    unittest.main()

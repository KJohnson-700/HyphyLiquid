"""Tests for AI advisory guardrails."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.ai_advisory import make_advisory_packet, validate_advisory  # noqa: E402


class TestAIAdvisory(unittest.TestCase):
    def test_valid_btc_advice_can_be_execution_eligible_but_not_an_order(self):
        packet = make_advisory_packet(
            symbol="BTC",
            deterministic_route={"execution_allowed": True},
            indicators={},
            risk={},
        )

        decision = validate_advisory(
            packet,
            {
                "symbol": "BTC",
                "action": "watch_playbook",
                "playbook": "btc_b_failed_reclaim_ask_heavy",
                "confidence": 0.82,
                "rationale": "Regime, tape, and risk context agree.",
                "evidence": {"regime": "range_wide", "tape": "ask_heavy", "risk": "within caps"},
            },
        )

        self.assertTrue(decision.allowed_for_execution)
        self.assertEqual(decision.action, "watch_playbook")
        self.assertEqual(decision.warnings, ())

    def test_ai_execution_request_is_ignored(self):
        packet = make_advisory_packet(
            symbol="BTC",
            deterministic_route={"execution_allowed": True},
            indicators={},
            risk={},
        )

        decision = validate_advisory(
            packet,
            {
                "symbol": "BTC",
                "action": "watch_playbook",
                "playbook": "btc_b_failed_reclaim_ask_heavy",
                "confidence": 0.9,
                "execute": True,
                "evidence": {"regime": "trend_up", "tape": "ask_heavy", "risk": "within caps"},
            },
        )

        self.assertFalse(decision.allowed_for_execution)
        self.assertIn("execution request ignored; AI advisory cannot place orders", decision.warnings)

    def test_research_symbol_is_coerced_to_paper_only(self):
        packet = make_advisory_packet(
            symbol="HYPE",
            deterministic_route={"execution_allowed": False},
            indicators={},
            risk={},
        )

        decision = validate_advisory(
            packet,
            {
                "symbol": "HYPE",
                "action": "watch_playbook",
                "playbook": "hype_b_range_scalp_research",
                "confidence": 0.95,
                "evidence": {"regime": "range_wide", "tape": "side_b", "risk": "research"},
            },
        )

        self.assertEqual(decision.action, "paper_only")
        self.assertFalse(decision.allowed_for_execution)

    def test_missing_evidence_blocks_execution_eligibility(self):
        packet = make_advisory_packet(
            symbol="BTC",
            deterministic_route={"execution_allowed": True},
            indicators={},
            risk={},
        )

        decision = validate_advisory(
            packet,
            {
                "symbol": "BTC",
                "action": "maintain",
                "confidence": 0.99,
                "evidence": {"regime": "range_wide"},
            },
        )

        self.assertFalse(decision.allowed_for_execution)
        self.assertTrue(any("missing evidence keys" in warning for warning in decision.warnings))


if __name__ == "__main__":
    unittest.main()

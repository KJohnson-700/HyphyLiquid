"""Tests for AI advisory model comparison."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.compare_ai_advisory_models import build_comparison  # noqa: E402


class TestAIAdvisoryModelCompare(unittest.TestCase):
    def test_ranks_clean_evidenced_model_above_warning_heavy_model(self):
        rows = [
            {
                "model_id": "claude-sonnet",
                "action": "stand_down",
                "confidence": 0.76,
                "rationale": "Regime and tape conflict, so standing down is the cleaner operational choice.",
                "evidence": {"regime": "mixed", "tape": "thin", "risk": "not worth it"},
                "allowed_for_execution": False,
                "warnings": [],
            },
            {
                "model_id": "claude-opus",
                "action": "watch_playbook",
                "confidence": 1.0,
                "rationale": "Buy now.",
                "evidence": {"regime": "trend"},
                "allowed_for_execution": False,
                "warnings": ["missing evidence keys: ['risk', 'tape']", "execution request ignored"],
            },
        ]

        report = build_comparison(rows)

        self.assertEqual(report["ranking"][0]["model_id"], "claude-sonnet")
        self.assertGreater(report["models"]["claude-sonnet"]["avg_score"], report["models"]["claude-opus"]["avg_score"])


if __name__ == "__main__":
    unittest.main()

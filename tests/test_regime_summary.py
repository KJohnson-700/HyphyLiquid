"""Focused tests for the pure helpers in scripts/run_regime_summary.py.

The end-to-end run reads data/cascades.jsonl + 1m candles, which is exercised
by the rebuild cycle. These tests cover the deterministic helpers that drive
the per-rebuild report (BTC watch pocket, ETH rejected lane, HYPE pocket,
safety gate) so we can refactor them safely.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Make `src` and `scripts` importable
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import run_regime_summary as rrs  # noqa: E402


class TestEThRejectedLane(unittest.TestCase):
    def test_no_trades(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.jsonl"
            p.write_text("", encoding="utf-8")
            result = rrs._step4_eth_rejected_lane_from(path=p)
            self.assertFalse(result["any_bucket_crossed"])
            self.assertEqual(result["trade_count"], 0)
            self.assertEqual(result["buckets"], [])

    def test_no_bucket_crosses_gate(self) -> None:
        trades = [
            {"symbol": "ETH", "variant": "baseline_fade", "side": "A", "return_pct": -0.01},
            {"symbol": "ETH", "variant": "baseline_fade", "side": "A", "return_pct": 0.02},
            {"symbol": "ETH", "variant": "reclaim_fade", "side": "B", "return_pct": -0.005},
            {"symbol": "ETH", "variant": "reclaim_fade", "side": "B", "return_pct": 0.01},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                for t in trades:
                    f.write(json.dumps(t) + "\n")
            result = rrs._step4_eth_rejected_lane_from(path=p)
            self.assertFalse(result["any_bucket_crossed"])
            self.assertEqual(result["trade_count"], 4)
            self.assertEqual(len(result["buckets"]), 2)

    def test_bucket_with_high_n_and_pf_and_median_does_not_cross_yet(self) -> None:
        """A small-N bucket with high PF + positive median should not cross the gate."""
        # n=10, all wins, PF=inf, med=+0.5 -> n < 100 so promotion_crossed=False
        trades = [{"symbol": "ETH", "variant": "x", "side": "B", "return_pct": 0.5} for _ in range(10)]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                for t in trades:
                    f.write(json.dumps(t) + "\n")
            result = rrs._step4_eth_rejected_lane_from(path=p)
            self.assertFalse(result["any_bucket_crossed"])
            self.assertEqual(result["buckets"][0]["n"], 10)
            self.assertFalse(result["buckets"][0]["promotion_crossed"])

    def test_large_bucket_with_high_pf_and_median_does_cross(self) -> None:
        # n=120, all +0.1, PF=inf, med=+0.1 -> all three conditions met
        trades = [{"symbol": "ETH", "variant": "x", "side": "B", "return_pct": 0.1} for _ in range(120)]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                for t in trades:
                    f.write(json.dumps(t) + "\n")
            result = rrs._step4_eth_rejected_lane_from(path=p)
            self.assertTrue(result["any_bucket_crossed"])
            self.assertEqual(result["buckets"][0]["n"], 120)
            self.assertTrue(result["buckets"][0]["promotion_crossed"])


class TestSafetyGate(unittest.TestCase):
    def test_no_violations(self) -> None:
        cascades = [{"symbol": "HYPE"}, {"symbol": "SOL"}]
        classifications = [
            {"execution_allowed": False, "regime": "range_normal", "response": "x", "route_action": "watch", "route_lane": "alt"},
            {"execution_allowed": False, "regime": "range_wide", "response": "x", "route_action": "watch", "route_lane": "alt"},
        ]
        result = rrs._step6_safety_gate(classifications, cascades)
        self.assertTrue(result["gate_holds"])
        self.assertEqual(result["violation_count"], 0)

    def test_violation_detected(self) -> None:
        cascades = [{"symbol": "HYPE", "side": "B"}]
        classifications = [
            {"execution_allowed": True, "regime": "range_normal", "response": "x", "route_action": "watch", "route_lane": "alt"},
        ]
        result = rrs._step6_safety_gate(classifications, cascades)
        self.assertFalse(result["gate_holds"])
        self.assertEqual(result["violation_count"], 1)
        self.assertEqual(result["violations"][0]["symbol"], "HYPE")

    def test_v1_symbols_ignored(self) -> None:
        cascades = [{"symbol": "BTC"}, {"symbol": "ETH"}]
        classifications = [
            {"execution_allowed": True, "regime": "x", "response": "x", "route_action": "x", "route_lane": "x"},
            {"execution_allowed": True, "regime": "x", "response": "x", "route_action": "x", "route_lane": "x"},
        ]
        result = rrs._step6_safety_gate(classifications, cascades)
        self.assertTrue(result["gate_holds"])
        self.assertEqual(result["violation_count"], 0)


class TestHypeResearchPocket(unittest.TestCase):
    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Force both focused + alt paths to miss
            focused = Path(td) / "focused.jsonl"
            alt = Path(td) / "alt.jsonl"
            result = rrs._step5_hype_research_pocket_from(focused_path=focused, alt_path=alt)
            self.assertEqual(result["trade_count"], 0)
            self.assertEqual(result["buckets"], {})

    def test_buckets_by_band_width_pct(self) -> None:
        # 3 compressed (all losses), 2 normal (mixed), 1 wide (win)
        trades = [
            {"symbol": "HYPE", "side": "B", "band_width_pct": 0.2, "net_return_pct": -0.1, "exit_reason": "stop"},
            {"symbol": "HYPE", "side": "B", "band_width_pct": 0.3, "net_return_pct": -0.05, "exit_reason": "stop"},
            {"symbol": "HYPE", "side": "B", "band_width_pct": 0.4, "net_return_pct": -0.02, "exit_reason": "stop"},
            {"symbol": "HYPE", "side": "B", "band_width_pct": 0.7, "net_return_pct": +0.05, "exit_reason": "tp"},
            {"symbol": "HYPE", "side": "B", "band_width_pct": 0.9, "net_return_pct": -0.03, "exit_reason": "stop"},
            {"symbol": "HYPE", "side": "B", "band_width_pct": 1.5, "net_return_pct": +0.20, "exit_reason": "tp"},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "focused.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                for t in trades:
                    f.write(json.dumps(t) + "\n")
            result = rrs._step5_hype_research_pocket_from(focused_path=p, alt_path=Path(td) / "missing.jsonl")
            self.assertEqual(result["trade_count"], 6)
            self.assertIn("compressed", result["buckets"])
            self.assertIn("normal", result["buckets"])
            self.assertIn("wide", result["buckets"])
            self.assertEqual(result["buckets"]["compressed"]["n"], 3)
            self.assertEqual(result["buckets"]["normal"]["n"], 2)
            self.assertEqual(result["buckets"]["wide"]["n"], 1)
            # Compressed: 3 losses, WR=0, PF=0
            self.assertEqual(result["buckets"]["compressed"]["win_rate"], 0.0)
            self.assertEqual(result["buckets"]["compressed"]["profit_factor"], 0.0)


class TestBtcWatchPocket(unittest.TestCase):
    def test_missing_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = rrs._step3_btc_watch_pocket_from(json_path=Path(td) / "missing.json")
            self.assertEqual(result["status"], "missing")

    def test_empty_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trailing.json"
            p.write_text("[]", encoding="utf-8")
            result = rrs._step3_btc_watch_pocket_from(json_path=p)
            self.assertEqual(result["status"], "no_eligible_rows")

    def test_best_row_selected(self) -> None:
        rows = [
            {
                "symbol": "BTC",
                "variant": "failed_reclaim_continuation",
                "horizon": 240,
                "stop_model": "event_vwap",
                "vwap_buffer_bps": 15.0,
                "activation_r": 1.5,
                "trail_bps": 10.0,
                "n": 37,
                "profit_factor": 1.09,
                "avg_net_return_pct": 0.0114,
                "median_net_return_pct": 0.0896,
                "activation_rate": 0.5135,
                "initial_stop_rate": 0.3784,
            },
            {
                "symbol": "BTC",
                "variant": "reclaim_fade",
                "horizon": 120,
                "stop_model": "fixed_bps",
                "config_initial_stop_bps": 30.0,
                "activation_r": 2.0,
                "trail_bps": 25.0,
                "n": 200,  # larger n
                "profit_factor": 0.6,  # but bad PF
                "avg_net_return_pct": -0.01,
                "median_net_return_pct": -0.02,
                "activation_rate": 0.1,
                "initial_stop_rate": 0.5,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trailing.json"
            p.write_text(json.dumps(rows), encoding="utf-8")
            result = rrs._step3_btc_watch_pocket_from(json_path=p)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["best"]["variant"], "failed_reclaim_continuation")
            self.assertEqual(result["best"]["n"], 37)
            self.assertEqual(result["best"]["pf"], 1.09)

    def test_promotion_gate_flags_met(self) -> None:
        rows = [
            {
                "symbol": "BTC",
                "variant": "failed_reclaim_continuation",
                "horizon": 240,
                "stop_model": "event_vwap",
                "vwap_buffer_bps": 15.0,
                "activation_r": 1.5,
                "trail_bps": 10.0,
                "n": 150,
                "profit_factor": 2.0,
                "avg_net_return_pct": 0.05,
                "median_net_return_pct": 0.02,
                "activation_rate": 0.5,
                "initial_stop_rate": 0.3,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trailing.json"
            p.write_text(json.dumps(rows), encoding="utf-8")
            result = rrs._step3_btc_watch_pocket_from(json_path=p)
            gate = result["promotion_gate"]
            self.assertTrue(gate["n_met"])
            self.assertTrue(gate["pf_met"])
            self.assertTrue(gate["median_met"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())

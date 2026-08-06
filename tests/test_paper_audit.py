"""Tests for paper simulation audit reporting."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_paper_audit import (  # noqa: E402
    _current_gate_records,
    _gate_bucket_key,
    _summarize_current_gate_only,
    build_audit,
    render_markdown,
    write_audit,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class TestPaperAudit(unittest.TestCase):
    def test_build_audit_summarizes_decisions_and_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_jsonl(
                data_dir / "paper_decisions_20260805.jsonl",
                [
                    {
                        "decision_ts": "2026-08-05T00:00:00+00:00",
                        "cascade_key": "BTC|B|1",
                        "symbol": "BTC",
                        "side": "B",
                        "lane": "btc_eth_trailing_resolution",
                        "action": "watch",
                        "paper_scope": "v1_paper",
                        "execution_allowed": True,
                        "route_reason": "ok",
                        "candle_regime": {},
                        "liquidation_response": {},
                        "decision": "open_position",
                        "reason": "BTC B-side failed-reclaim continuation with ask_heavy book",
                        "paper_id": "paper-btc",
                    },
                    {
                        "decision_ts": "2026-08-05T00:01:00+00:00",
                        "cascade_key": "BTC|A|2",
                        "symbol": "BTC",
                        "side": "A",
                        "lane": "none",
                        "action": "reject",
                        "paper_scope": "none",
                        "execution_allowed": False,
                        "route_reason": "no route",
                        "candle_regime": {},
                        "liquidation_response": {},
                        "decision": "reject",
                        "reason": "no route",
                    },
                ],
            )
            _write_jsonl(
                data_dir / "paper_positions_20260805.jsonl",
                [
                    {
                        "event": "opened",
                        "paper_id": "paper-btc",
                        "paper_scope": "v1_paper",
                        "cascade_key": "BTC|B|1",
                        "symbol": "BTC",
                        "side": "B",
                        "lane": "btc_eth_trailing_resolution",
                        "direction": "long",
                        "event_ts": "2026-08-05T00:00:00+00:00",
                        "entry_ts": "2026-08-05T00:03:00+00:00",
                        "entry_idx": 3,
                        "entry_price": 100.0,
                        "notional_usd": 1000.0,
                        "risk_usd": 10.0,
                        "bracket": {
                            "entry_price": 100.0,
                            "initial_stop_price": 99.0,
                            "target_price": None,
                            "activation_price": 102.0,
                            "trail_bps": 10.0,
                            "max_hold_minutes": 240,
                            "stop_slippage_bps": 2.0,
                            "round_trip_cost_bps": 8.0,
                        },
                        "metadata": {
                            "paper_gate": "top_book_imbalance=ask_heavy",
                            "top_book_imbalance_bucket": "ask_heavy",
                        },
                    },
                    {
                        "event": "mark",
                        "paper_id": "paper-btc",
                        "paper_scope": "v1_paper",
                        "cascade_key": "BTC|B|1",
                        "symbol": "BTC",
                        "side": "B",
                        "lane": "btc_eth_trailing_resolution",
                        "direction": "long",
                        "event_ts": "2026-08-05T00:00:00+00:00",
                        "entry_ts": "2026-08-05T00:03:00+00:00",
                        "entry_idx": 3,
                        "entry_price": 100.0,
                        "notional_usd": 1000.0,
                        "risk_usd": 10.0,
                        "bracket": {},
                        "metadata": {},
                        "fill": {
                            "paper_id": "paper-btc",
                            "status": "closed",
                            "exit_ts": "2026-08-05T00:10:00+00:00",
                            "exit_price": 102.0,
                            "exit_reason": "trailing_stop",
                            "gross_return_pct": 2.0,
                            "net_return_pct": 1.92,
                            "pnl_usd": 19.2,
                            "r_multiple": 1.92,
                            "mae_pct": 0.1,
                            "mfe_pct": 2.5,
                            "bars_held": 7,
                            "final_trailing_stop": 102.0,
                        },
                    },
                ],
            )

            audit = build_audit(data_dir, recent_limit=5)

            self.assertEqual(audit["decision_summary"]["total"], 2)
            self.assertEqual(audit["opened_positions"], 1)
            self.assertEqual(audit["fill_summary"]["closed"], 1)
            self.assertEqual(audit["fill_summary"]["profit_factor"], float("inf"))
            self.assertEqual(audit["anomalies"], [])
            self.assertIn("Paper Simulation Audit", render_markdown(audit))

    def test_audit_flags_missing_open_row_and_bad_btc_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_jsonl(
                data_dir / "paper_decisions_20260805.jsonl",
                [
                    {
                        "decision": "open_position",
                        "paper_id": "paper-missing",
                        "symbol": "BTC",
                        "paper_scope": "v1_paper",
                        "lane": "btc_eth_trailing_resolution",
                    },
                    {
                        "decision": "open_position",
                        "paper_id": "paper-bad-gate",
                        "symbol": "BTC",
                        "paper_scope": "v1_paper",
                        "lane": "btc_eth_trailing_resolution",
                    },
                ],
            )
            _write_jsonl(
                data_dir / "paper_positions_20260805.jsonl",
                [
                    {
                        "event": "opened",
                        "paper_id": "paper-bad-gate",
                        "symbol": "BTC",
                        "paper_scope": "v1_paper",
                        "notional_usd": 1000,
                        "risk_usd": 10,
                        "metadata": {"top_book_imbalance_bucket": "neutral"},
                    }
                ],
            )

            audit = build_audit(data_dir, recent_limit=5)

            issues = {row["issue"] for row in audit["anomalies"]}
            self.assertIn("open_position decision has no opened row", issues)
            self.assertIn("BTC paper open missing ask_heavy imbalance gate", issues)
            self.assertIn("opened position has no mark/fill row", issues)

    def test_write_audit_outputs_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            audit = build_audit(data_dir)

            json_path, md_path = write_audit(audit, data_dir)

            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["decision_summary"]["total"], 0)

    def test_current_gate_only_separates_gated_from_legacy(self):
        # Build a small dataset: one gated BTC ask_heavy, one gated ETH (with
        # a non-default gate string to verify the bucket key), and one legacy
        # HYPE position with no gate metadata.
        rows = [
            # Gated BTC ask_heavy: opened, mark/closed with a win
            {
                "event": "opened",
                "paper_id": "p-btc-1",
                "paper_scope": "v1_paper",
                "symbol": "BTC",
                "side": "B",
                "lane": "btc_eth_trailing_resolution",
                "direction": "long",
                "entry_price": 100.0,
                "entry_ts": "2026-08-05T00:03:00+00:00",
                "metadata": {
                    "paper_gate": "top_book_imbalance=ask_heavy",
                    "top_book_imbalance_bucket": "ask_heavy",
                },
            },
            {
                "event": "mark",
                "paper_id": "p-btc-1",
                "paper_scope": "v1_paper",
                "symbol": "BTC",
                "side": "B",
                "lane": "btc_eth_trailing_resolution",
                "direction": "long",
                "metadata": {
                    "paper_gate": "top_book_imbalance=ask_heavy",
                },
                "fill": {
                    "status": "closed",
                    "exit_ts": "2026-08-05T00:10:00+00:00",
                    "exit_reason": "trailing_stop",
                    "gross_return_pct": 2.0,
                    "net_return_pct": 1.92,
                    "pnl_usd": 19.2,
                    "r_multiple": 1.92,
                },
            },
            # Gated BTC ask_heavy: opened, mark/closed with a loss
            {
                "event": "opened",
                "paper_id": "p-btc-2",
                "paper_scope": "v1_paper",
                "symbol": "BTC",
                "side": "B",
                "lane": "btc_eth_trailing_resolution",
                "direction": "long",
                "entry_price": 110.0,
                "entry_ts": "2026-08-05T00:13:00+00:00",
                "metadata": {
                    "paper_gate": "top_book_imbalance=ask_heavy",
                    "top_book_imbalance_bucket": "ask_heavy",
                },
            },
            {
                "event": "mark",
                "paper_id": "p-btc-2",
                "paper_scope": "v1_paper",
                "symbol": "BTC",
                "side": "B",
                "lane": "btc_eth_trailing_resolution",
                "direction": "long",
                "metadata": {
                    "paper_gate": "top_book_imbalance=ask_heavy",
                },
                "fill": {
                    "status": "closed",
                    "exit_ts": "2026-08-05T00:18:00+00:00",
                    "exit_reason": "initial_stop",
                    "gross_return_pct": -1.0,
                    "net_return_pct": -1.08,
                    "pnl_usd": -10.8,
                    "r_multiple": -1.08,
                },
            },
            # Legacy HYPE position: no paper_gate in metadata (should be excluded)
            {
                "event": "opened",
                "paper_id": "p-hype-legacy",
                "paper_scope": "research_paper",
                "symbol": "HYPE",
                "side": "B",
                "lane": "alt_range_liq_scalp",
                "direction": "short",
                "entry_price": 57.0,
                "entry_ts": "2026-08-05T14:07:00+00:00",
                "metadata": {
                    "band_lower": 56.82,
                    "band_mid": 56.99,
                    "band_upper": 57.16,
                    "band_width_pct": 0.59,
                },
            },
            {
                "event": "mark",
                "paper_id": "p-hype-legacy",
                "paper_scope": "research_paper",
                "symbol": "HYPE",
                "side": "B",
                "lane": "alt_range_liq_scalp",
                "direction": "short",
                "metadata": {},
                "fill": {
                    "status": "closed",
                    "exit_ts": "2026-08-05T14:08:00+00:00",
                    "exit_reason": "initial_stop",
                    "gross_return_pct": -0.13,
                    "net_return_pct": -0.21,
                    "pnl_usd": -18.9,
                    "r_multiple": -1.89,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_jsonl(data_dir / "paper_positions_20260805.jsonl", rows)

            audit = build_audit(data_dir, recent_limit=5)
            current = audit["current_gate_only"]

            # Counts: 4 gated records (2 opened + 2 mark for BTC), 2 non-gated
            self.assertEqual(current["gated_records"], 4)
            self.assertEqual(current["non_gated_records"], 2)
            self.assertEqual(current["gated_opened"], 2)
            self.assertEqual(current["gated_closed"], 2)
            self.assertEqual(current["gated_open_now"], 0)

            # One bucket key (v1_paper|BTC|btc_eth_trailing_resolution|top_book_imbalance=ask_heavy)
            self.assertEqual(len(current["by_bucket"]), 1)
            bucket_key = "v1_paper|BTC|btc_eth_trailing_resolution|top_book_imbalance=ask_heavy"
            self.assertIn(bucket_key, current["by_bucket"])
            row = current["by_bucket"][bucket_key]
            self.assertEqual(row["n"], 2)
            # 1 win (1.92), 1 loss (-1.08) -> WR 50%
            self.assertEqual(row["win_rate_pct"], 50.0)
            # avg = (1.92 - 1.08) / 2 = 0.42, med = (1.92 - 1.08) / 2 = 0.42 (mean of 2)
            self.assertAlmostEqual(row["avg_net_return_pct"], 0.42, places=3)
            self.assertAlmostEqual(row["median_net_return_pct"], 0.42, places=3)
            # PF = 1.92 / 1.08
            self.assertAlmostEqual(row["profit_factor"], 1.92 / 1.08, places=3)
            # Exit reasons count
            self.assertEqual(row["exit_reasons"]["trailing_stop"], 1)
            self.assertEqual(row["exit_reasons"]["initial_stop"], 1)

            # BTC ask_heavy aggregate: same numbers as the single bucket
            agg = current["btc_ask_heavy_aggregate"]
            self.assertEqual(agg["n"], 2)
            self.assertEqual(agg["win_rate_pct"], 50.0)
            # by_lane breakout should also contain the BTC ask_heavy key
            self.assertIn(bucket_key, current["btc_ask_heavy"])

            # Rendered markdown mentions the gate section and BTC ask_heavy
            md = render_markdown(audit)
            self.assertIn("## Current Gate Only", md)
            self.assertIn("BTC ask_heavy", md)
            self.assertIn(bucket_key, md)

    def test_current_gate_only_empty_when_no_gates(self):
        # Only legacy / non-gated positions
        rows = [
            {
                "event": "opened",
                "paper_id": "p-legacy",
                "paper_scope": "v1_paper",
                "symbol": "HYPE",
                "side": "B",
                "lane": "alt_range_liq_scalp",
                "direction": "short",
                "entry_price": 57.0,
                "metadata": {"band_width_pct": 0.5},  # no paper_gate
            },
            {
                "event": "mark",
                "paper_id": "p-legacy",
                "paper_scope": "v1_paper",
                "symbol": "HYPE",
                "side": "B",
                "lane": "alt_range_liq_scalp",
                "metadata": {},
                "fill": {
                    "status": "closed",
                    "net_return_pct": -0.2,
                    "r_multiple": -1.0,
                    "exit_reason": "initial_stop",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_jsonl(data_dir / "paper_positions_20260805.jsonl", rows)
            audit = build_audit(data_dir, recent_limit=5)
            current = audit["current_gate_only"]
            self.assertEqual(current["gated_records"], 0)
            self.assertEqual(current["gated_closed"], 0)
            self.assertEqual(current["non_gated_records"], 2)
            self.assertEqual(current["by_bucket"], {})
            self.assertEqual(current["btc_ask_heavy_aggregate"], {})

    def test_current_gate_records_filters_correctly(self):
        rows = [
            {"metadata": {"paper_gate": "top_book_imbalance=ask_heavy"}},
            {"metadata": {"paper_gate": ""}},  # empty string -> excluded
            {"metadata": {"paper_gate": None}},  # None -> excluded
            {"metadata": {"band_width_pct": 0.5}},  # missing paper_gate -> excluded
            {"metadata": "not a dict"},  # bad shape -> excluded
            {"metadata": {}},  # empty -> excluded
        ]
        kept = _current_gate_records(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["metadata"]["paper_gate"], "top_book_imbalance=ask_heavy")

    def test_gate_bucket_key_format(self):
        row = {
            "paper_scope": "v1_paper",
            "symbol": "BTC",
            "lane": "btc_eth_trailing_resolution",
            "metadata": {"paper_gate": "top_book_imbalance=ask_heavy"},
        }
        self.assertEqual(
            _gate_bucket_key(row),
            ("v1_paper", "BTC", "btc_eth_trailing_resolution", "top_book_imbalance=ask_heavy"),
        )


if __name__ == "__main__":
    unittest.main()

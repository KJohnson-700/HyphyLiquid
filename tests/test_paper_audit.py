"""Tests for paper simulation audit reporting."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_paper_audit import build_audit, render_markdown, write_audit  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

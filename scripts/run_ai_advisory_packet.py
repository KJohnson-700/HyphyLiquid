"""Build and optionally validate AI advisory packets.

This script prepares bounded context for an AI regime/tape co-pilot. It does
not call an LLM and it does not execute trades. If a model response JSON is
provided, the response is validated and appended as advisory-only output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.ai_advisory import make_advisory_packet, validate_advisory  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_jsonl_tail(path: Path, limit: int = 1) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder()
    idx = 0
    objects: list[dict] = []
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, next_idx = decoder.raw_decode(text, idx)
        except ValueError:
            break
        if isinstance(obj, dict):
            objects.append(obj)
        idx = next_idx
    if objects:
        return objects[-limit:]
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]


def _latest_regime_summary(data_dir: Path) -> dict:
    regime_dir = data_dir / "regime_log"
    if not regime_dir.exists():
        return {}
    rows: list[dict] = []
    for path in sorted(regime_dir.glob("regime_summary_*.jsonl")):
        rows.extend(_load_jsonl_tail(path, limit=1000))
    return rows[-1] if rows else {}


def _top_filter_bucket(symbol: str, data_dir: Path) -> dict:
    payload = _load_json(data_dir / "btc_eth_filter_diagnostics.json")
    if isinstance(payload, dict):
        rows = payload.get("diagnostics", [])
    else:
        rows = payload
    if not isinstance(rows, list):
        return {}
    sym = symbol.upper()
    matching = [r for r in rows if isinstance(r, dict) and f"symbol={sym}" in str(r.get("bucket", ""))]
    if not matching:
        return {}
    matching.sort(key=lambda r: (float(r.get("profit_factor") or 0), int(r.get("n") or 0)), reverse=True)
    return matching[0]


def _paper_audit_context(data_dir: Path) -> dict:
    audit = _load_json(data_dir / "paper_audit_latest.json")
    if not isinstance(audit, dict):
        return {}
    fill = audit.get("fill_summary", {}) if isinstance(audit.get("fill_summary"), dict) else {}
    return {
        "decisions": audit.get("decision_summary", {}).get("total") if isinstance(audit.get("decision_summary"), dict) else None,
        "opened_positions": audit.get("opened_positions"),
        "open_now": audit.get("open_now"),
        "anomalies": len(audit.get("anomalies", [])) if isinstance(audit.get("anomalies"), list) else None,
        "legacy_warnings": len(audit.get("legacy_warnings", [])) if isinstance(audit.get("legacy_warnings"), list) else None,
        "closed": fill.get("closed"),
        "profit_factor": fill.get("profit_factor"),
        "median_net_return_pct": fill.get("median_net_return_pct"),
        "avg_r_multiple": fill.get("avg_r_multiple"),
    }


def build_packet(symbol: str, data_dir: Path = DATA_DIR) -> dict:
    """Build an advisory packet for the given symbol from latest local diagnostics."""
    sym = symbol.upper()
    regime = _latest_regime_summary(data_dir)
    filter_bucket = _top_filter_bucket(sym, data_dir)
    paper = _paper_audit_context(data_dir)
    btc_watch = regime.get("btc_watch_pocket", {}) if isinstance(regime.get("btc_watch_pocket"), dict) else {}
    latest_regime_metadata = regime.get("rebuild_metadata", {}) if isinstance(regime.get("rebuild_metadata"), dict) else {}
    safety = regime.get("safety_gate", {}) if isinstance(regime.get("safety_gate"), dict) else {}

    deterministic_allowed = sym in {"BTC", "ETH"} and sym == "BTC"
    route = {
        "execution_allowed": deterministic_allowed,
        "current_rule": "BTC side=B failed_reclaim_continuation + top_book_imbalance=ask_heavy"
        if sym == "BTC"
        else "collect_or_reject",
        "safety_gate_holds": safety.get("gate_holds", True),
    }
    disagreement = {
        "simple_rule_pf": btc_watch.get("profit_factor"),
        "filtered_bucket": filter_bucket,
        "paper_pf": paper.get("profit_factor"),
        "paper_median_net_return_pct": paper.get("median_net_return_pct"),
        "note": "AI may recommend stand_down/maintain/watch_playbook only; no execution.",
    }
    tape = {
        "btc_watch_pocket": btc_watch,
        "top_filter_bucket": filter_bucket,
    }
    risk = {
        "bankroll_usd": 1000,
        "max_risk_per_trade_usd": 10,
        "max_leverage": 10,
        "v1_symbols": ["BTC", "ETH"],
        "research_only": ["SOL", "HYPE", "DOGE", "BNB", "xyz:GOLD", "xyz:SILVER"],
        "order_execution_source": "risk.py + OrderManager only",
    }
    news = {
        "status": "not_loaded",
        "instruction": "Daily news cron may summarize macro/exchange/news context here; news is context, not a trigger.",
    }
    packet = make_advisory_packet(
        symbol=sym,
        deterministic_route=route,
        indicators={
            "latest_regime_summary": latest_regime_metadata,
            "classification_health": regime.get("classification_health", {}),
            "paper_audit": paper,
        },
        disagreement=disagreement,
        tape=tape,
        risk=risk,
        news=news,
    )
    return packet.to_dict()


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--response-json", type=Path, help="Optional AI response JSON to validate.")
    args = parser.parse_args()

    packet_dict = build_packet(args.symbol, args.data_dir)
    packet_path = args.data_dir / "ai_advisory_packet_latest.json"
    packet_path.write_text(json.dumps(packet_dict, indent=2, sort_keys=True), encoding="utf-8")

    out = {"packet": str(packet_path), "symbol": packet_dict["symbol"], "scope": packet_dict["scope"]}
    if args.response_json:
        raw = _load_json(args.response_json)
        if not isinstance(raw, dict):
            raise SystemExit(f"response JSON is missing or invalid: {args.response_json}")
        from src.strategy.ai_advisory import AdvisoryPacket  # noqa: WPS433

        packet = AdvisoryPacket(**packet_dict)
        decision = validate_advisory(packet, raw)
        decision_path = args.data_dir / "ai_advisory_decisions.jsonl"
        _append_jsonl(decision_path, decision.to_dict())
        out["decision"] = decision.to_dict()
        out["decision_log"] = str(decision_path)

    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

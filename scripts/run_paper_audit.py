"""Audit live-like paper decisions and simulated fills.

The report is intentionally ledger-based: it reads the append-only paper
decision and position files and produces a compact trust report without
touching live execution.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.paper_decision_loop import (  # noqa: E402
    BTC_REQUIRED_IMBALANCE_BUCKET,
    MAX_PAPER_NOTIONAL_USD,
    TARGET_RISK_USD,
)

DATA_DIR = PROJECT_ROOT / "data"


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_many(pattern: str, data_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(data_dir.glob(pattern)):
        rows.extend(_load_jsonl(path))
    return rows


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4) if values else 0.0


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _profit_factor(values: list[float]) -> float | None:
    wins = sum(v for v in values if v > 0)
    losses = abs(sum(v for v in values if v < 0))
    if losses == 0:
        return None if wins == 0 else float("inf")
    return round(wins / losses, 4)


def _bucket_key(row: dict) -> str:
    return "|".join(
        [
            str(row.get("symbol", "")),
            str(row.get("paper_scope", "")),
            str(row.get("lane", "")),
            str(row.get("decision", "")),
        ]
    )


def _position_open_index(position_rows: Iterable[dict]) -> dict[str, dict]:
    opened: dict[str, dict] = {}
    for row in position_rows:
        if row.get("event") == "opened" and row.get("paper_id"):
            opened[str(row["paper_id"])] = row
    return opened


def _latest_fill_index(position_rows: Iterable[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in position_rows:
        paper_id = row.get("paper_id")
        fill = row.get("fill")
        if paper_id and isinstance(fill, dict):
            latest[str(paper_id)] = row
    return latest


def _closed_fills(position_rows: Iterable[dict]) -> list[dict]:
    fills = []
    for row in position_rows:
        fill = row.get("fill")
        if isinstance(fill, dict) and fill.get("status") == "closed":
            fills.append({"position": row, "fill": fill})
    return fills


def _summarize_decisions(decisions: list[dict]) -> dict:
    by_decision = Counter(str(row.get("decision", "")) for row in decisions)
    by_symbol_decision = Counter(f"{row.get('symbol')}|{row.get('decision')}" for row in decisions)
    by_bucket = Counter(_bucket_key(row) for row in decisions)
    reject_reasons = Counter(str(row.get("reason", "")) for row in decisions if row.get("decision") != "open_position")
    return {
        "total": len(decisions),
        "by_decision": dict(sorted(by_decision.items())),
        "by_symbol_decision": dict(sorted(by_symbol_decision.items())),
        "by_symbol_scope_lane_decision": dict(sorted(by_bucket.items())),
        "top_reject_reasons": dict(reject_reasons.most_common(12)),
    }


def _summarize_fills(closed: list[dict]) -> dict:
    net = [float(row["fill"].get("net_return_pct", 0) or 0) for row in closed]
    pnl = [float(row["fill"].get("pnl_usd", 0) or 0) for row in closed]
    r_values = [float(row["fill"].get("r_multiple", 0) or 0) for row in closed]
    wins = [v for v in net if v > 0]
    exit_reasons = Counter(str(row["fill"].get("exit_reason", "")) for row in closed)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_lane: dict[str, list[float]] = defaultdict(list)
    for row in closed:
        pos = row["position"]
        value = float(row["fill"].get("net_return_pct", 0) or 0)
        by_symbol[str(pos.get("symbol", ""))].append(value)
        by_lane[str(pos.get("lane", ""))].append(value)

    def pack(values: list[float]) -> dict:
        return {
            "n": len(values),
            "win_rate_pct": round(len([v for v in values if v > 0]) / len(values) * 100, 2) if values else 0.0,
            "avg_net_return_pct": _avg(values),
            "median_net_return_pct": _median(values),
            "profit_factor": _profit_factor(values),
        }

    return {
        "closed": len(closed),
        "win_rate_pct": round(len(wins) / len(net) * 100, 2) if net else 0.0,
        "avg_net_return_pct": _avg(net),
        "median_net_return_pct": _median(net),
        "profit_factor": _profit_factor(net),
        "total_pnl_usd": round(sum(pnl), 4),
        "avg_r_multiple": _avg(r_values),
        "median_r_multiple": _median(r_values),
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "by_symbol": {k: pack(v) for k, v in sorted(by_symbol.items())},
        "by_lane": {k: pack(v) for k, v in sorted(by_lane.items())},
    }


def _audit_anomalies(decisions: list[dict], position_rows: list[dict]) -> list[dict]:
    opened = _position_open_index(position_rows)
    latest = _latest_fill_index(position_rows)
    anomalies: list[dict] = []
    for decision in decisions:
        paper_id = decision.get("paper_id")
        if decision.get("decision") == "open_position" and paper_id and str(paper_id) not in opened:
            anomalies.append({"severity": "high", "paper_id": paper_id, "issue": "open_position decision has no opened row"})
        if decision.get("symbol") == "BTC" and decision.get("decision") == "open_position":
            opened_row = opened.get(str(paper_id), {}) if paper_id else {}
            metadata = opened_row.get("metadata", {}) if isinstance(opened_row.get("metadata"), dict) else {}
            metadata_bucket = metadata.get("top_book_imbalance_bucket")
            if metadata_bucket is not None and metadata_bucket != BTC_REQUIRED_IMBALANCE_BUCKET:
                anomalies.append(
                    {
                        "severity": "high",
                        "paper_id": paper_id,
                        "issue": f"BTC paper open missing {BTC_REQUIRED_IMBALANCE_BUCKET} imbalance gate",
                    }
                )
    for paper_id, row in opened.items():
        if paper_id not in latest:
            anomalies.append({"severity": "medium", "paper_id": paper_id, "issue": "opened position has no mark/fill row"})
        symbol = str(row.get("symbol", ""))
        if row.get("paper_scope") == "v1_paper" and symbol not in {"BTC", "ETH"}:
            anomalies.append({"severity": "high", "paper_id": paper_id, "issue": "non-v1 symbol opened in v1_paper scope"})
        notional = float(row.get("notional_usd", 0) or 0)
        risk = float(row.get("risk_usd", 0) or 0)
        if notional > MAX_PAPER_NOTIONAL_USD + 0.01:
            anomalies.append({"severity": "high", "paper_id": paper_id, "issue": "paper notional exceeds max cap"})
        if risk > TARGET_RISK_USD + 0.05:
            anomalies.append({"severity": "high", "paper_id": paper_id, "issue": "paper risk exceeds target cap"})
    return anomalies


def _legacy_warnings(decisions: list[dict], position_rows: list[dict]) -> list[dict]:
    opened = _position_open_index(position_rows)
    warnings = []
    for decision in decisions:
        paper_id = decision.get("paper_id")
        if decision.get("symbol") != "BTC" or decision.get("decision") != "open_position" or not paper_id:
            continue
        opened_row = opened.get(str(paper_id), {})
        metadata = opened_row.get("metadata", {}) if isinstance(opened_row.get("metadata"), dict) else {}
        if "top_book_imbalance_bucket" not in metadata and "paper_gate" not in metadata:
            warnings.append(
                {
                    "paper_id": paper_id,
                    "issue": "BTC paper open predates current ask_heavy gate metadata",
                }
            )
    return warnings


def _recent_rows(decisions: list[dict], position_rows: list[dict], limit: int) -> dict:
    latest = _latest_fill_index(position_rows)
    opened = _position_open_index(position_rows)
    recent_decisions = decisions[-limit:] if limit > 0 else []
    recent_opens = []
    for row in reversed(position_rows):
        if row.get("event") != "opened":
            continue
        paper_id = str(row.get("paper_id", ""))
        mark = latest.get(paper_id, {}).get("fill", {})
        recent_opens.append(
            {
                "paper_id": paper_id,
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "direction": row.get("direction"),
                "lane": row.get("lane"),
                "entry_price": row.get("entry_price"),
                "stop_price": row.get("bracket", {}).get("initial_stop_price") if isinstance(row.get("bracket"), dict) else None,
                "activation_price": row.get("bracket", {}).get("activation_price") if isinstance(row.get("bracket"), dict) else None,
                "risk_usd": row.get("risk_usd"),
                "notional_usd": row.get("notional_usd"),
                "metadata": row.get("metadata", {}),
                "latest_fill": mark,
            }
        )
        if len(recent_opens) >= limit:
            break
    return {
        "decisions": recent_decisions,
        "opened_positions": recent_opens,
        "opened_count": len(opened),
    }


def build_audit(data_dir: Path = DATA_DIR, *, recent_limit: int = 12) -> dict:
    """Build a paper audit report from local append-only ledgers."""
    decisions = _load_many("paper_decisions_*.jsonl", data_dir)
    position_rows = _load_many("paper_positions_*.jsonl", data_dir)
    closed = _closed_fills(position_rows)
    opened = _position_open_index(position_rows)
    latest = _latest_fill_index(position_rows)
    open_now = [
        paper_id
        for paper_id in opened
        if latest.get(paper_id, {}).get("fill", {}).get("status") != "closed"
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "decision_summary": _summarize_decisions(decisions),
        "fill_summary": _summarize_fills(closed),
        "opened_positions": len(opened),
        "open_now": len(open_now),
        "open_paper_ids": sorted(open_now),
        "anomalies": _audit_anomalies(decisions, position_rows),
        "legacy_warnings": _legacy_warnings(decisions, position_rows),
        "recent": _recent_rows(decisions, position_rows, recent_limit),
    }


def render_markdown(audit: dict) -> str:
    """Render the audit into a compact Markdown report."""
    decisions = audit["decision_summary"]
    fills = audit["fill_summary"]
    anomalies = audit["anomalies"]
    legacy_warnings = audit.get("legacy_warnings", [])
    lines = [
        "# Paper Simulation Audit",
        "",
        f"Generated: `{audit['generated_at']}`",
        f"Data dir: `{audit['data_dir']}`",
        "",
        "## Summary",
        "",
        f"- Decisions: {decisions['total']}",
        f"- Opened positions: {audit['opened_positions']}",
        f"- Closed fills: {fills['closed']}",
        f"- Open now: {audit['open_now']}",
        f"- Net PnL: ${fills['total_pnl_usd']:.2f}",
        f"- PF: {fills['profit_factor']}",
        f"- Win rate: {fills['win_rate_pct']}%",
        f"- Avg/median net return: {fills['avg_net_return_pct']}% / {fills['median_net_return_pct']}%",
        f"- Avg/median R: {fills['avg_r_multiple']} / {fills['median_r_multiple']}",
        "",
        "## Exit Reasons",
        "",
    ]
    if fills["exit_reasons"]:
        lines.extend(f"- {reason}: {count}" for reason, count in fills["exit_reasons"].items())
    else:
        lines.append("- none")
    lines.extend(["", "## By Symbol", ""])
    for symbol, row in fills["by_symbol"].items():
        lines.append(
            f"- {symbol}: n={row['n']}, PF={row['profit_factor']}, WR={row['win_rate_pct']}%, "
            f"avg/med={row['avg_net_return_pct']}%/{row['median_net_return_pct']}%"
        )
    if not fills["by_symbol"]:
        lines.append("- none")
    lines.extend(["", "## Top Reject Reasons", ""])
    for reason, count in decisions["top_reject_reasons"].items():
        lines.append(f"- {count}x: {reason}")
    if not decisions["top_reject_reasons"]:
        lines.append("- none")
    lines.extend(["", "## Anomalies", ""])
    if anomalies:
        for row in anomalies:
            lines.append(f"- [{row['severity']}] {row.get('paper_id', 'n/a')}: {row['issue']}")
    else:
        lines.append("- none")
    lines.extend(["", "## Legacy Warnings", ""])
    if legacy_warnings:
        for row in legacy_warnings:
            lines.append(f"- {row.get('paper_id', 'n/a')}: {row['issue']}")
    else:
        lines.append("- none")
    lines.extend(["", "## Recent Opens", ""])
    for row in audit["recent"]["opened_positions"]:
        fill = row.get("latest_fill", {})
        metadata = row.get("metadata", {}) if isinstance(row.get("metadata"), dict) else {}
        lines.append(
            f"- {row['paper_id']} {row['symbol']} {row['direction']} entry={row['entry_price']} "
            f"stop={row['stop_price']} activation={row['activation_price']} "
            f"risk=${row['risk_usd']} notional=${row['notional_usd']} "
            f"gate={metadata.get('paper_gate', 'n/a')} status={fill.get('status', 'unmarked')} "
            f"exit={fill.get('exit_reason')} net={fill.get('net_return_pct')}% R={fill.get('r_multiple')}"
        )
    if not audit["recent"]["opened_positions"]:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def write_audit(audit: dict, data_dir: Path = DATA_DIR) -> tuple[Path, Path]:
    """Write latest JSON and Markdown audit artifacts."""
    json_path = data_dir / "paper_audit_latest.json"
    md_path = data_dir / "paper_audit_latest.md"
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(audit), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--recent", type=int, default=12)
    args = parser.parse_args()
    audit = build_audit(args.data_dir, recent_limit=args.recent)
    json_path, md_path = write_audit(audit, args.data_dir)
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "decisions": audit["decision_summary"]["total"],
        "opened": audit["opened_positions"],
        "closed": audit["fill_summary"]["closed"],
        "open_now": audit["open_now"],
        "anomalies": len(audit["anomalies"]),
        "legacy_warnings": len(audit["legacy_warnings"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

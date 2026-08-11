"""One-command execution canary for tomorrow's bot build.

Default mode is paper: consume fresh cascades, update the live-like paper
ledger, run the audit, and write a compact canary status artifact.

Live mode is intentionally guarded. It does not place orders unless the
operator passes an explicit CLI arming flag and sets an env arming flag.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
STATUS_JSON = DATA_DIR / "execution_canary_status.json"
STATUS_MD = DATA_DIR / "execution_canary_status.md"
LIVE_ARM_ENV = "HYPHYLIQUID_LIVE_TRADING_ENABLED"


@dataclass(frozen=True)
class LiveGuard:
    """Live execution guard result."""

    allowed: bool
    reason: str
    env: str
    cli_armed: bool
    env_armed: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_live_guard(*, cli_armed: bool, env: str | None = None, env_armed: str | None = None) -> LiveGuard:
    """Return whether live mode is armed enough to place real orders."""
    normalized_env = (env or os.getenv("HYPERLIQUID_ENV", "testnet")).strip().lower()
    armed_value = env_armed if env_armed is not None else os.getenv(LIVE_ARM_ENV, "")
    armed = armed_value.strip() == "1"
    if normalized_env != "mainnet":
        return LiveGuard(False, "HYPERLIQUID_ENV must be mainnet for live mode", normalized_env, cli_armed, armed)
    if not armed:
        return LiveGuard(False, f"{LIVE_ARM_ENV}=1 is required for live mode", normalized_env, cli_armed, armed)
    if not cli_armed:
        return LiveGuard(False, "--i-understand-real-orders is required for live mode because these are real orders", normalized_env, cli_armed, armed)
    return LiveGuard(True, "live mode armed", normalized_env, cli_armed, armed)


def _write_status(status: dict) -> tuple[Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    STATUS_MD.write_text(_render_status(status), encoding="utf-8")
    return STATUS_JSON, STATUS_MD


def _render_status(status: dict) -> str:
    lines = [
        "# Execution Canary Status",
        "",
        f"Generated: `{status['generated_at']}`",
        f"Mode: `{status['mode']}`",
        f"Live armed: `{status['live_guard']['allowed']}`",
        f"Guard reason: {status['live_guard']['reason']}",
        "",
        "## Paper Pass",
        "",
    ]
    paper = status.get("paper_pass") or {}
    if paper:
        lines.extend(
            [
                f"- Decisions written: {paper.get('decisions_written', 0)}",
                f"- Positions opened: {paper.get('positions_opened', 0)}",
                f"- Positions closed: {paper.get('positions_closed', 0)}",
                f"- Processed total: {paper.get('processed_total', 0)}",
            ]
        )
    else:
        lines.append("- skipped")
    intent = status.get("eth_intent_preview") or {}
    lines.extend(["", "## ETH Intent Preview", ""])
    if intent:
        lines.extend(
            [
                f"- Eligible: {intent.get('eligible')}",
                f"- Reason: {intent.get('reason')}",
                f"- Lane: `{intent.get('lane', '')}`",
                f"- Symbol/side: `{intent.get('symbol', '')}` / `{intent.get('side', '')}`",
                f"- Entry/SL/TP: `{intent.get('entry_px', '')}` / `{intent.get('sl_px', '')}` / `{intent.get('tp_px', '')}`",
                f"- Timeout exit: `{intent.get('timeout_exit', '')}`",
            ]
        )
    else:
        lines.append("- skipped")
    timeout_preview = status.get("timeout_supervisor_preview") or {}
    lines.extend(["", "## Timeout Supervisor", ""])
    if timeout_preview:
        lines.extend(
            [
                f"- Eligible: {timeout_preview.get('eligible')}",
                f"- Action: `{timeout_preview.get('action', '')}`",
                f"- Reason: {timeout_preview.get('reason')}",
                f"- Position: `{timeout_preview.get('position_id', '')}`",
                f"- Due at: `{timeout_preview.get('due_at', '')}`",
            ]
        )
    else:
        lines.append("- skipped")
    audit = status.get("audit") or {}
    lines.extend(["", "## Audit", ""])
    if audit:
        lines.extend(
            [
                f"- Decisions: {audit.get('decisions', 0)}",
                f"- Opened: {audit.get('opened', 0)}",
                f"- Closed: {audit.get('closed', 0)}",
                f"- Open now: {audit.get('open_now', 0)}",
                f"- Anomalies: {audit.get('anomalies', 0)}",
                f"- Markdown: `{audit.get('markdown', '')}`",
            ]
        )
    else:
        lines.append("- skipped")
    lines.extend(["", "## Next Action", "", f"- {status['next_action']}"])
    return "\n".join(lines) + "\n"


def run_paper_canary(*, max_new: int, recent: int) -> dict:
    """Run one live-like paper pass plus audit."""
    from scripts.paper_decision_loop import run_once
    from scripts.run_paper_audit import build_audit, write_audit
    from src.execution.paper_intents import build_latest_eth_intent_preview
    from src.execution.position_supervisor import build_latest_eth_timeout_preview

    paper_pass = run_once(max_new=max_new)
    audit = build_audit(DATA_DIR, recent_limit=recent)
    _, md_path = write_audit(audit, DATA_DIR)
    eth_intent_preview = build_latest_eth_intent_preview(DATA_DIR)
    timeout_supervisor_preview = build_latest_eth_timeout_preview(DATA_DIR)
    return {
        "paper_pass": paper_pass,
        "eth_intent_preview": eth_intent_preview,
        "timeout_supervisor_preview": timeout_supervisor_preview,
        "audit": {
            "decisions": audit["decision_summary"]["total"],
            "opened": audit["opened_positions"],
            "closed": audit["fill_summary"]["closed"],
            "open_now": audit["open_now"],
            "anomalies": len(audit["anomalies"]),
            "markdown": str(md_path),
        },
    }


def build_status(*, mode: str, guard: LiveGuard, paper_payload: dict | None = None) -> dict:
    """Build the persisted canary status payload."""
    paper_payload = paper_payload or {}
    anomalies = (paper_payload.get("audit") or {}).get("anomalies", 0)
    if mode == "live" and not guard.allowed:
        next_action = "Live mode refused by guard; run paper canary or arm deliberately after review."
    elif anomalies:
        next_action = "Paper audit has anomalies; fix those before any live canary."
    elif mode == "paper":
        next_action = "Paper canary completed; review audit, then keep daemon running for fresh live-like decisions."
    else:
        next_action = "Live mode is armed; order placement still belongs to the explicit live execution step."
    return {
        "generated_at": _utc_now(),
        "mode": mode,
        "live_guard": asdict(guard),
        "paper_pass": paper_payload.get("paper_pass"),
        "eth_intent_preview": paper_payload.get("eth_intent_preview"),
        "timeout_supervisor_preview": paper_payload.get("timeout_supervisor_preview"),
        "audit": paper_payload.get("audit"),
        "next_action": next_action,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--max-new", type=int, default=250)
    parser.add_argument("--recent", type=int, default=12)
    parser.add_argument("--i-understand-real-orders", action="store_true")
    args = parser.parse_args()

    guard = check_live_guard(cli_armed=args.i_understand_real_orders)
    paper_payload: dict | None = None
    if args.mode == "paper":
        paper_payload = run_paper_canary(max_new=args.max_new, recent=args.recent)
    status = build_status(mode=args.mode, guard=guard, paper_payload=paper_payload)
    json_path, md_path = _write_status(status)
    print(json.dumps({
        "status_json": str(json_path),
        "status_md": str(md_path),
        "mode": args.mode,
        "live_allowed": guard.allowed,
        "next_action": status["next_action"],
    }, sort_keys=True))
    return 0 if args.mode == "paper" or guard.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())

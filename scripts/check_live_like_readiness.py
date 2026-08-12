"""One-command PASS/HOLD/BLOCK readiness check for live-like bot work."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
STATUS_JSON = DATA_DIR / "live_like_readiness_status.json"
STATUS_MD = DATA_DIR / "live_like_readiness_status.md"


@dataclass(frozen=True)
class ReadinessCheck:
    """Single readiness check result."""

    name: str
    status: str
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def load_json(path: Path) -> dict:
    """Load a JSON object, returning an empty dict on missing/invalid files."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def evaluate_readiness(data_dir: Path) -> dict:
    """Build a blunt live-like readiness verdict from current status artifacts."""
    canary = load_json(data_dir / "execution_canary_status.json")
    audit = canary.get("audit") or {}
    proof_gate = canary.get("execution_proof_gate") or {}
    reconciliation = canary.get("reconciliation") or {}
    intent = canary.get("eth_intent_preview") or {}
    supervisor = canary.get("timeout_supervisor_preview") or {}
    paper_pass = canary.get("paper_pass") or {}

    checks = [
        _check_canary_exists(canary),
        _check_proof_gate(proof_gate),
        _check_audit(audit),
        _check_reconciliation(reconciliation),
        _check_eth_intent(intent),
        _check_supervisor(supervisor),
        _check_fresh_processing(paper_pass),
    ]
    verdict = _rollup(checks)
    next_action = _next_action(verdict, checks, canary)
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "next_action": next_action,
        "checks": [check.to_dict() for check in checks],
        "summary": {
            "canary_generated_at": canary.get("generated_at"),
            "paper_decisions": audit.get("decisions"),
            "paper_opened": audit.get("opened"),
            "paper_closed": audit.get("closed"),
            "paper_open_now": audit.get("open_now"),
            "paper_anomalies": audit.get("anomalies"),
            "proof_passed": proof_gate.get("passed"),
            "proof_status": proof_gate.get("proof_status"),
            "protective_stop_visible": proof_gate.get("protective_stop_visible"),
            "reconciliation_status": reconciliation.get("status"),
            "eth_intent_eligible": intent.get("eligible"),
            "supervisor_eligible": supervisor.get("eligible"),
        },
    }
    return status


def write_status(status: dict, data_dir: Path) -> tuple[Path, Path]:
    """Persist readiness status as JSON and Markdown."""
    data_dir.mkdir(parents=True, exist_ok=True)
    json_path = data_dir / STATUS_JSON.name
    md_path = data_dir / STATUS_MD.name
    json_path.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_status(status), encoding="utf-8")
    return json_path, md_path


def render_status(status: dict) -> str:
    """Render a compact Markdown readiness report."""
    summary = status.get("summary") or {}
    lines = [
        "# Live-Like Readiness",
        "",
        f"Generated: `{status.get('generated_at', '')}`",
        f"Verdict: `{status.get('verdict', '')}`",
        f"Next action: {status.get('next_action', '')}",
        "",
        "## Summary",
        "",
        f"- Canary generated: `{summary.get('canary_generated_at', '')}`",
        f"- Paper decisions/opened/closed/open: `{summary.get('paper_decisions', '')}` / `{summary.get('paper_opened', '')}` / `{summary.get('paper_closed', '')}` / `{summary.get('paper_open_now', '')}`",
        f"- Paper anomalies: `{summary.get('paper_anomalies', '')}`",
        f"- Proof gate: `{summary.get('proof_passed', '')}` / `{summary.get('proof_status', '')}` / stop visible `{summary.get('protective_stop_visible', '')}`",
        f"- Reconciliation: `{summary.get('reconciliation_status', '')}`",
        f"- ETH intent eligible: `{summary.get('eth_intent_eligible', '')}`",
        f"- Supervisor eligible: `{summary.get('supervisor_eligible', '')}`",
        "",
        "## Checks",
        "",
    ]
    for check in status.get("checks") or []:
        lines.append(f"- `{check.get('status')}` {check.get('name')}: {check.get('detail')}")
    return "\n".join(lines) + "\n"


def _check_canary_exists(canary: dict) -> ReadinessCheck:
    if not canary:
        return ReadinessCheck("execution_canary", "BLOCK", "execution_canary_status.json is missing or invalid")
    return ReadinessCheck("execution_canary", "PASS", f"latest canary generated at {canary.get('generated_at')}")


def _check_proof_gate(proof_gate: dict) -> ReadinessCheck:
    if proof_gate.get("passed") is True:
        return ReadinessCheck("execution_proof_gate", "PASS", "bracket proof passed with visible stop and flat cleanup")
    return ReadinessCheck("execution_proof_gate", "BLOCK", proof_gate.get("reason") or "proof gate missing or failed")


def _check_audit(audit: dict) -> ReadinessCheck:
    if not audit:
        return ReadinessCheck("paper_audit", "BLOCK", "paper audit missing from canary")
    anomalies = int(audit.get("anomalies") or 0)
    open_now = int(audit.get("open_now") or 0)
    if anomalies:
        return ReadinessCheck("paper_audit", "BLOCK", f"{anomalies} paper audit anomalies need cleanup")
    if open_now:
        return ReadinessCheck("paper_audit", "HOLD", f"{open_now} paper positions still open; supervise to resolution")
    return ReadinessCheck("paper_audit", "PASS", "no anomalies and no open paper positions")


def _check_reconciliation(reconciliation: dict) -> ReadinessCheck:
    if not reconciliation:
        return ReadinessCheck("reconciliation", "BLOCK", "reconciliation status missing")
    if reconciliation.get("blocking"):
        return ReadinessCheck("reconciliation", "BLOCK", "exchange/local reconciliation is blocking")
    if reconciliation.get("status") == "skipped":
        return ReadinessCheck("reconciliation", "HOLD", "exchange snapshot not fetched; ok for paper, not enough for live")
    return ReadinessCheck("reconciliation", "PASS", f"reconciliation status {reconciliation.get('status')}")


def _check_eth_intent(intent: dict) -> ReadinessCheck:
    if intent.get("eligible") is True:
        return ReadinessCheck("eth_intent", "PASS", "latest ETH paper position can become a bracket intent")
    return ReadinessCheck("eth_intent", "HOLD", intent.get("reason") or "no active ETH intent right now")


def _check_supervisor(supervisor: dict) -> ReadinessCheck:
    if supervisor.get("eligible") is True:
        return ReadinessCheck("timeout_supervisor", "PASS", f"supervisor action {supervisor.get('action')}")
    return ReadinessCheck("timeout_supervisor", "HOLD", supervisor.get("reason") or "no active position needs supervision")


def _check_fresh_processing(paper_pass: dict) -> ReadinessCheck:
    if not paper_pass:
        return ReadinessCheck("paper_processing", "BLOCK", "paper pass missing")
    processed = int(paper_pass.get("processed_total") or 0)
    if processed <= 0:
        return ReadinessCheck("paper_processing", "HOLD", "paper canary did not process any rows")
    return ReadinessCheck("paper_processing", "PASS", f"paper canary processed {processed} rows")


def _rollup(checks: list[ReadinessCheck]) -> str:
    statuses = {check.status for check in checks}
    if "BLOCK" in statuses:
        return "BLOCK"
    if "HOLD" in statuses:
        return "HOLD"
    return "PASS"


def _next_action(verdict: str, checks: list[ReadinessCheck], canary: dict) -> str:
    for check in checks:
        if check.status == "BLOCK":
            return f"Fix {check.name}: {check.detail}"
    for check in checks:
        if check.status == "HOLD":
            return f"Continue paper/live-like loop: {check.detail}"
    return canary.get("next_action") or "All readiness checks pass; operator review is the next gate."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    status = evaluate_readiness(data_dir)
    json_path, md_path = write_status(status, data_dir)
    print(json.dumps({
        "verdict": status["verdict"],
        "next_action": status["next_action"],
        "status_json": str(json_path),
        "status_md": str(md_path),
    }, sort_keys=True))
    return 0 if status["verdict"] in {"PASS", "HOLD"} else 2


if __name__ == "__main__":
    raise SystemExit(main())

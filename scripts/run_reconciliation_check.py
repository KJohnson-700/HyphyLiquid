"""Run a HyphyLiquid local-vs-Hyperliquid reconciliation check.

Default mode is offline/local-only and never calls Hyperliquid. Pass
`--fetch-exchange` to read user state and open orders through the official SDK.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
STATUS_JSON = DATA_DIR / "reconciliation_status.json"
STATUS_MD = DATA_DIR / "reconciliation_status.md"


def _write_status(status: dict) -> tuple[Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    STATUS_MD.write_text(_render_status(status), encoding="utf-8")
    return STATUS_JSON, STATUS_MD


def _render_status(status: dict) -> str:
    lines = [
        "# Reconciliation Status",
        "",
        f"Generated: `{status.get('generated_at', '')}`",
        f"Status: `{status.get('status', '')}`",
        f"Action: `{status.get('action', '')}`",
        f"Blocking: `{status.get('blocking', '')}`",
        "",
        "## Findings",
        "",
    ]
    findings = status.get("findings") or []
    if not findings:
        lines.append("- none")
    for finding in findings:
        lines.append(
            f"- `{finding.get('severity')}` `{finding.get('code')}` "
            f"{finding.get('symbol') or ''}: {finding.get('message')}"
        )
    snapshot = status.get("exchange_snapshot")
    lines.extend(["", "## Exchange Snapshot", ""])
    if not snapshot:
        lines.append("- not fetched")
    else:
        lines.extend(
            [
                f"- User: `{snapshot.get('user', '')}`",
                f"- Account value: `{snapshot.get('account_value', '')}`",
                f"- Positions: `{len(snapshot.get('positions') or [])}`",
                f"- Orders: `{len(snapshot.get('orders') or [])}`",
            ]
        )
    return "\n".join(lines) + "\n"


def _fetch_snapshot(env: str, user: str):
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from src.execution.reconciler import fetch_exchange_snapshot

    base_url = constants.TESTNET_API_URL if env == "testnet" else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    return fetch_exchange_snapshot(info, user)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-exchange", action="store_true")
    parser.add_argument("--env", choices=("testnet", "mainnet"), default=os.getenv("HYPERLIQUID_ENV", "testnet"))
    parser.add_argument("--user", default=os.getenv("HYPERLIQUID_WALLET_ADDRESS", ""))
    args = parser.parse_args()

    from src.execution.reconciler import build_reconciliation_preview

    snapshot = None
    if args.fetch_exchange:
        if not args.user:
            raise SystemExit("--user or HYPERLIQUID_WALLET_ADDRESS is required with --fetch-exchange")
        snapshot = _fetch_snapshot(args.env, args.user)
    status = build_reconciliation_preview(DATA_DIR, exchange_snapshot=snapshot)
    json_path, md_path = _write_status(status)
    print(json.dumps({
        "status_json": str(json_path),
        "status_md": str(md_path),
        "status": status["status"],
        "action": status["action"],
        "blocking": status["blocking"],
    }, sort_keys=True))
    return 2 if status["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

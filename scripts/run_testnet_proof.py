"""Guarded testnet proof for reconciliation and reduce-only close mechanics."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
STATUS_JSON = DATA_DIR / "testnet_proof_status.json"
STATUS_MD = DATA_DIR / "testnet_proof_status.md"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(PROJECT_ROOT / ".env")


def _clients(env: str):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    base_url = constants.TESTNET_API_URL if env == "testnet" else constants.MAINNET_API_URL
    pk = os.getenv("HYPERLIQUID_PRIVATE_KEY", "").strip()
    user = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "").strip()
    if not pk or not user:
        raise RuntimeError("HYPERLIQUID_PRIVATE_KEY and HYPERLIQUID_WALLET_ADDRESS are required")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    wallet = Account.from_key(pk)
    if wallet.address.lower() != user.lower():
        raise RuntimeError(f"private key derives to {wallet.address}, wallet env says {user}")
    return Info(base_url, skip_ws=True), Exchange(wallet, base_url), user


def _write_status(status: dict) -> tuple[Path, Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    STATUS_MD.write_text(_render_status(status), encoding="utf-8")
    return STATUS_JSON, STATUS_MD


def _render_status(status: dict) -> str:
    lines = [
        "# Testnet Proof Status",
        "",
        f"Generated: `{status.get('generated_at', '')}`",
        f"Mode: `{status.get('mode', '')}`",
        f"Status: `{status.get('status', '')}`",
        f"Orders sent: `{len(status.get('orders_sent') or [])}`",
        "",
        "## Guard",
        "",
    ]
    guard = status.get("guard") or {}
    lines.extend(
        [
            f"- Allowed: `{guard.get('allowed')}`",
            f"- Reason: {guard.get('reason')}",
            f"- Env: `{guard.get('env')}`",
            f"- Execute orders: `{guard.get('execute_orders')}`",
        ]
    )
    plan = status.get("plan") or {}
    lines.extend(["", "## Plan", ""])
    for step in plan.get("steps") or []:
        lines.append(f"- {step}")
    recon = status.get("reconciliation") or status.get("before_reconciliation") or {}
    lines.extend(["", "## Reconciliation", ""])
    if recon:
        lines.extend(
            [
                f"- Status: `{recon.get('status', '')}`",
                f"- Action: `{recon.get('action', '')}`",
                f"- Blocking: `{recon.get('blocking', '')}`",
            ]
        )
    else:
        lines.append("- not run")
    orders_sent = status.get("orders_sent") or []
    if orders_sent:
        lines.extend(["", "## Order Proof", ""])
        for order in orders_sent:
            request = order.get("request") or {}
            response = order.get("response") or {}
            response_status = response.get("status") or response.get("response", {}).get("status") or ""
            lines.append(
                f"- {order.get('type')}: `{request.get('name') or request.get('symbol')}` "
                f"size `{request.get('sz') or request.get('size_coin')}` "
                f"limit `{request.get('limit_px', '')}` status `{response_status}`"
            )
    after_close = status.get("after_close") or {}
    if after_close:
        lines.extend(["", "## After Close", ""])
        lines.append(f"- Positions: `{len(after_close.get('positions') or [])}`")
        lines.append(f"- Open orders: `{len(after_close.get('orders') or [])}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-exchange", action="store_true")
    parser.add_argument("--execute-testnet-orders", action="store_true")
    parser.add_argument("--i-understand-testnet-orders", action="store_true")
    parser.add_argument("--env", choices=("testnet", "mainnet"), default=os.getenv("HYPERLIQUID_ENV", "testnet"))
    parser.add_argument("--user", default=os.getenv("HYPERLIQUID_WALLET_ADDRESS", ""))
    parser.add_argument("--symbol", default="ETH")
    parser.add_argument("--side", choices=("long", "short"), default="short")
    parser.add_argument("--size-coin", type=float, default=0.01)
    parser.add_argument("--slippage-bps", type=float, default=20.0)
    args = parser.parse_args()

    from src.execution.testnet_proof import (
        build_testnet_proof_plan,
        check_testnet_proof_guard,
        run_fetch_only_proof,
        run_order_proof,
        utc_now_iso,
    )

    _load_dotenv()
    env = args.env or os.getenv("HYPERLIQUID_ENV", "testnet")
    user = args.user or os.getenv("HYPERLIQUID_WALLET_ADDRESS", "")
    guard = check_testnet_proof_guard(
        env=env,
        user=user,
        fetch_exchange=args.fetch_exchange,
        execute_orders=args.execute_testnet_orders,
        cli_armed=args.i_understand_testnet_orders,
    )
    plan = build_testnet_proof_plan(guard)
    status = {
        "generated_at": utc_now_iso(),
        "mode": plan["mode"],
        "status": "guard_refused" if not guard.allowed else "planned",
        "guard": guard.to_dict(),
        "plan": plan,
        "orders_sent": [],
    }
    if guard.allowed and args.fetch_exchange:
        info, exchange, resolved_user = _clients(env)
        if args.execute_testnet_orders:
            status.update(
                run_order_proof(
                    info,
                    exchange,
                    user=resolved_user,
                    symbol=args.symbol,
                    side=args.side,
                    size_coin=args.size_coin,
                    slippage_bps=args.slippage_bps,
                )
            )
        else:
            status.update(run_fetch_only_proof(info, user=resolved_user))
            status["status"] = "fetched"
    json_path, md_path = _write_status(status)
    print(json.dumps({
        "status_json": str(json_path),
        "status_md": str(md_path),
        "mode": status["mode"],
        "status": status["status"],
        "orders_sent": len(status.get("orders_sent") or []),
    }, sort_keys=True))
    return 0 if guard.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())

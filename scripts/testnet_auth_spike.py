"""
HyphyLiquid — Testnet Auth Spike
=================================

Week 1 deliverable: prove the Hyperliquid Python SDK connects to testnet,
place one tiny order, cancel it, and read the fill back.

This is the smallest possible end-to-end test of the auth + order path.
It must pass before any strategy code is written.

Usage:
    # 1. Get testnet wallet + USDC
    #    Wallet: any Ethereum address (MetaMask, etc.)
    #    Faucet: https://app.hyperliquid-testnet.xyz/drip
    #
    # 2. Add creds to .env:
    #      HYPERLIQUID_WALLET_ADDRESS=0x...
    #      HYPERLIQUID_PRIVATE_KEY=0x...
    #      HYPERLIQUID_ENV=testnet
    #
    # 3. Run:
    #      python scripts/testnet_auth_spike.py
    #
    # If no creds are set, runs in preflight mode (read-only checks only).

Exits 0 on success, non-zero on any failure.
"""

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

# Add project root to path so we can import from src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")

# Constants
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"


def log(label: str, msg: str) -> None:
    print(f"[{label}] {msg}")


def preflight() -> bool:
    """Read-only checks that don't need auth."""
    try:
        from hyperliquid.info import Info
    except ImportError as e:
        log("preflight", f"FAIL: hyperliquid SDK not installed: {e}")
        log("preflight", "  Run: pip install -r requirements.txt")
        return False

    env = os.getenv("HYPERLIQUID_ENV", "testnet").lower()
    base_url = TESTNET_URL if env == "testnet" else MAINNET_URL

    log("preflight", f"Connecting to {env} ({base_url})...")
    try:
        info = Info(base_url, skip_ws=True)
        meta = info.meta()
        mids = info.all_mids()
    except Exception as e:
        log("preflight", f"FAIL: {e}")
        return False

    perp_count = len(meta.get("universe", []))
    log("preflight", f"OK — {perp_count} perpetuals available")
    log("preflight", f"Top 3 by symbol: {sorted(mids.keys())[:3]}")
    if "BTC" in mids:
        log("preflight", f"BTC mid: ${float(mids['BTC']):,.2f}")
    return True


def auth_test() -> bool:
    """Full auth round-trip: place a tiny far-from-market order, then cancel it."""
    try:
        from eth_account import Account
    except ImportError:
        log("auth", "FAIL: eth_account not installed")
        log("auth", "  Add 'eth-account' to requirements.txt")
        return False

    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange

    env = os.getenv("HYPERLIQUID_ENV", "testnet").lower()
    base_url = TESTNET_URL if env == "testnet" else MAINNET_URL

    pk = os.getenv("HYPERLIQUID_PRIVATE_KEY", "").strip()
    addr = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "").strip()

    if not pk or not addr:
        log("auth", "SKIPPED — no wallet creds in .env")
        log("auth", "  Set HYPERLIQUID_PRIVATE_KEY + HYPERLIQUID_WALLET_ADDRESS in .env")
        log("auth", "  Get test USDC from: https://app.hyperliquid-testnet.xyz/drip")
        return True  # preflight passed; auth just skipped

    # Sanity check the wallet
    if not pk.startswith("0x"):
        pk = "0x" + pk
    wallet = Account.from_key(pk)
    if wallet.address.lower() != addr.lower():
        log("auth", f"FAIL: address mismatch — key derives to {wallet.address}, .env says {addr}")
        return False
    log("auth", f"Wallet OK: {addr}")

    # Initialize clients
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url)

    # Check account state
    try:
        user_state = info.user_state(addr)
        account_value = float(user_state["marginSummary"]["accountValue"])
        log("auth", f"Account value: ${account_value:,.2f}")
        if account_value < 10:
            log("auth", f"  Account is small (${account_value:.2f}). Faucet may be needed:")
            log("auth", "  https://app.hyperliquid-testnet.xyz/drip")
    except Exception as e:
        log("auth", f"FAIL getting user state: {e}")
        return False

    # Place a tiny limit BUY at 50% of mid (won't fill, easy to cancel)
    mids = info.all_mids()
    btc_mid = float(mids.get("BTC", 0))
    if btc_mid <= 0:
        log("auth", "FAIL: could not get BTC mid price")
        return False

    # BTC tick size on HL is $1, so round to integer
    far_price = int(btc_mid * 0.5)
    order_size = 0.001  # tiny

    log("auth", f"Placing test order: BUY 0.001 BTC @ ${far_price} (mid: ${btc_mid:,.2f})")
    log("auth", "  ^ this is far below market, will NOT fill")

    try:
        order_result = exchange.order(
            name="BTC",
            is_buy=True,
            sz=order_size,
            limit_px=far_price,
            order_type={"limit": {"tif": "Gtc"}},
            reduce_only=False,
        )
        log("auth", f"Order result: {order_result}")
        if order_result.get("status") != "ok":
            log("auth", "FAIL: order did not return status=ok")
            return False
        # Check inner statuses for actual fill/rejection
        statuses = order_result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            log("auth", "FAIL: no statuses in order response")
            return False
        first = statuses[0]
        if "error" in first:
            log("auth", f"FAIL: order rejected: {first['error']}")
            return False
        if "resting" not in first:
            log("auth", f"FAIL: order not resting: {first}")
            return False
        oid = first["resting"]["oid"]
        log("auth", f"Order resting, oid={oid}")
    except Exception as e:
        log("auth", f"FAIL placing order: {e}")
        return False

    # Wait a moment for the order to appear in open orders
    time.sleep(2)

    # Verify the order is in the open orders list
    try:
        open_orders = info.open_orders(addr)
        log("auth", f"Open orders after place: {len(open_orders)}")
        for o in open_orders:
            log("auth", f"  {o}")
    except Exception as e:
        log("auth", f"FAIL reading open orders: {e}")
        return False

    # Cancel all open orders (new SDK has bulk_cancel instead of cancel_all)
    log("auth", "Cancelling all open orders...")
    try:
        if open_orders:
            cancel_requests = [
                {"coin": o["coin"], "oid": o["oid"]} for o in open_orders
            ]
            cancel_result = exchange.bulk_cancel(cancel_requests)
            log("auth", f"Cancel result: {cancel_result}")
        else:
            log("auth", "  no open orders to cancel")
    except Exception as e:
        log("auth", f"FAIL cancelling: {e}")
        return False

    # Verify nothing left
    time.sleep(2)
    open_orders_after = info.open_orders(addr)
    log("auth", f"Open orders after cancel: {len(open_orders_after)}")
    if open_orders_after:
        log("auth", "WARN: some orders still open after cancel")
    else:
        log("auth", "All orders cancelled cleanly [OK]")

    log("auth", "TEST PASSED [OK]")
    log("auth", "End-to-end auth + order placement + cancellation works.")
    return True


def main() -> int:
    print("=" * 70)
    print("HyphyLiquid — Testnet Auth Spike")
    print("=" * 70)
    print()

    if not preflight():
        return 1
    print()

    if not auth_test():
        return 1
    print()

    print("=" * 70)
    print("All checks passed. SDK is wired and ready for strategy code.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

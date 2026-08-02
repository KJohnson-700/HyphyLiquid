"""Check balances on both addresses the user mentioned."""
import json
import sys
sys.path.insert(0, r"C:\Users\AbuBa\Desktop\HyphyLiquid")
from hyperliquid.info import Info

env_wallet = "0x4581139E9A8820f0867Fee74d4A224bFf9af0524"  # in .env
quicknode_addr = "0x966179487b7D09690Aeb8b88640B5e6D9a549C8B"  # what user typed on QN

print("=== TESTNET ===")
for label, addr in [("env (auth)", env_wallet), ("QuickNode form", quicknode_addr)]:
    print(f"\n{label}  {addr}")
    for env, base in [("testnet", "https://api.hyperliquid-testnet.xyz"),
                      ("mainnet", "https://api.hyperliquid.xyz")]:
        try:
            info = Info(base, skip_ws=True)
            perp = info.user_state(addr)
            av = perp.get("marginSummary", {}).get("accountValue", "0")
            print(f"  {env:8} perp: ${av}")
            spot = info.spot_user_state(addr)
            for b in spot.get("balances", []):
                print(f"  {env:8} spot: {b.get('coin')}={b.get('total')} (hold={b.get('hold')})")
        except Exception as e:
            print(f"  {env:8} ERROR: {e}")

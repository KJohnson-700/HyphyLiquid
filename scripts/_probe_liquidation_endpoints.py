"""Probe raw HL API for public liquidation data endpoints."""
import json
import requests

MAINNET = "https://api.hyperliquid.xyz/info"
TESTNET = "https://api.hyperliquid-testnet.xyz/info"

# Try a few common endpoint names that might exist
endpoints = [
    {"type": "liquidatedPositions"},
    {"type": "liquidations"},
    {"type": "recentLiquidations"},
    {"type": "allLiquidations"},
    {"type": "trades", "coin": "BTC", "limit": 5},  # baseline check
    {"type": "fundingHistory", "coin": "BTC", "startTime": 0, "endTime": 9999999999999},  # baseline
]

for url in (MAINNET, TESTNET):
    print(f"\n=== {url} ===")
    for ep in endpoints:
        try:
            r = requests.post(url, json=ep, timeout=15)
            try:
                data = r.json()
                if isinstance(data, list):
                    print(f"  {ep['type']:30} -> {r.status_code}  list[{len(data)}]  sample: {json.dumps(data[0])[:200] if data else 'empty'}")
                else:
                    print(f"  {ep['type']:30} -> {r.status_code}  {str(data)[:200]}")
            except Exception:
                print(f"  {ep['type']:30} -> {r.status_code}  (non-JSON) {r.text[:200]}")
        except Exception as e:
            print(f"  {ep['type']:30} -> ERROR: {e}")

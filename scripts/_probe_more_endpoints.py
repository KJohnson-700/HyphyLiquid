"""Probe more HL info endpoints to find liquidation data."""
import json
import requests

URL = "https://api.hyperliquid.xyz/info"

# More candidate endpoints to probe
candidates = [
    # Recent trades (QuickNode mentioned)
    {"type": "recentTrades", "coin": "BTC"},
    # Maybe with no coin
    {"type": "recentTrades"},
    # Predicted fundings
    {"type": "predictedFundings"},
    # Liquidatable (per Chainstack)
    {"type": "liquidatable", "user": "0x0000000000000000000000000000000000000000"},
    # Meta
    {"type": "meta"},
    # All mids
    {"type": "allMids"},
    # Asset contexts
    {"type": "metaAndAssetCtxs"},
]

for c in candidates:
    try:
        r = requests.post(URL, json=c, timeout=15)
        try:
            data = r.json()
            if isinstance(data, list):
                print(f"  {c['type']:25}  status={r.status_code}  list[{len(data)}]  sample: {json.dumps(data[0])[:250] if data else 'empty'}")
            elif isinstance(data, dict):
                print(f"  {c['type']:25}  status={r.status_code}  dict keys: {list(data.keys())[:10]}")
            else:
                print(f"  {c['type']:25}  status={r.status_code}  {str(data)[:200]}")
        except Exception:
            print(f"  {c['type']:25}  status={r.status_code}  (non-JSON) {r.text[:200]}")
    except Exception as e:
        print(f"  {c['type']:25}  ERROR: {e}")

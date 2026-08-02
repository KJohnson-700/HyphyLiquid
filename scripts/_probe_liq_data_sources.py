"""Probe public liquidation data sources for Hyperliquid."""
import json
import requests
from pathlib import Path

OUT = Path(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data\liquidation_sources")
OUT.mkdir(parents=True, exist_ok=True)

# 1. Moon Dev
print("=== Moon Dev (api.moondev.com) ===")
for tf in ("24h", "7d", "30d"):
    url = f"https://api.moondev.com/api/liquidations/{tf}.json"
    try:
        r = requests.get(url, timeout=20)
        print(f"  {tf:5}  status={r.status_code}  size={len(r.content)} bytes")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                print(f"    list[{len(data)}]  sample: {json.dumps(data[0])[:300] if data else 'empty'}")
                (OUT / f"moondev_{tf}.json").write_text(json.dumps(data[:200], indent=2))  # save sample
            else:
                print(f"    dict keys: {list(data.keys())[:10]}")
                (OUT / f"moondev_{tf}.json").write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  {tf:5}  ERROR: {e}")

# 2. HyperTracker (ht-api.coinmarketman.com)
print("\n=== HyperTracker (ht-api.coinmarketman.com) ===")
for url in [
    "https://ht-api.coinmarketman.com/api/external/fills/liquidation?coin=BTC&limit=5",
    "https://ht-api.coinmarketman.com/api/external/fills/liquidation?limit=5",
]:
    try:
        r = requests.get(url, timeout=20)
        print(f"  {url[:80]}  status={r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"    type={type(data).__name__}  sample: {json.dumps(data)[:500] if isinstance(data, (dict, list)) else data}")
    except Exception as e:
        print(f"  ERROR: {e}")

# 3. 0xArchive (api.0xarchive.io)
print("\n=== 0xArchive (api.0xarchive.io) ===")
# Try a recent 1-day window: 2026-08-01 to 2026-08-02
end_ms = 1722604800000  # placeholder
import time
now_ms = int(time.time() * 1000)
one_day_ago = now_ms - 24 * 60 * 60 * 1000
for coin in ("BTC", "ETH"):
    url = f"https://api.0xarchive.io/v1/hyperliquid/liquidations/{coin}?start={one_day_ago}&end={now_ms}"
    try:
        r = requests.get(url, timeout=20)
        print(f"  {coin} (24h)  status={r.status_code}  size={len(r.content)} bytes")
        if r.status_code == 200:
            try:
                data = r.json()
                print(f"    type={type(data).__name__}")
                if isinstance(data, dict):
                    print(f"    keys: {list(data.keys())[:10]}")
                elif isinstance(data, list):
                    print(f"    list[{len(data)}]  sample: {json.dumps(data[0])[:400] if data else 'empty'}")
            except Exception:
                print(f"    (non-JSON) {r.text[:300]}")
    except Exception as e:
        print(f"  ERROR: {e}")

# 4. nautilus_trader reference: HL has userEvents WebSocket with 'liquidation' event
# That's for your own wallet only - confirmed in their docs
print("\n=== Hyperliquid userEvents WebSocket ===")
print("  (Nautilus Trader docs confirm: HL emits 'liquidation' event in userEvents")
print("   subscription, but only for YOUR OWN wallet, not public. Need auth.)")

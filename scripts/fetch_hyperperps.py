"""Fetch and save HyperPerps heatmap snapshots for BTC and ETH."""
import json
import requests
from pathlib import Path

OUT = Path(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data\hyperperps")
OUT.mkdir(parents=True, exist_ok=True)

for coin in ("BTC", "ETH"):
    r = requests.get(f"https://trade.hyperperps.app/api/public/heatmap/{coin}", timeout=20)
    data = r.json()
    (OUT / f"{coin.lower()}_heatmap.json").write_text(json.dumps(data, indent=2))
    print(f"=== {coin} (saved) ===")
    for k, v in data.items():
        if k == "_meta":
            print(f"  _meta: {v}")
        elif k == "support":
            continue
        elif isinstance(v, list):
            print(f"  {k}: list[{len(v)}], sample[0]={str(v[0])[:200] if v else 'empty'}")
        elif isinstance(v, dict):
            print(f"  {k}: dict keys={list(v.keys())[:10]}, sample={str(list(v.values())[:3])[:200]}")
        else:
            print(f"  {k}: {v}")
    print()

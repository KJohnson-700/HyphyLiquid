"""Debug why no events overlap with candle window."""
import json
from pathlib import Path

import pandas as pd

c = pd.read_csv(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data\btc_candles_1h_7d_mainnet.csv")
c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
print("Candles BTC:")
print(f"  min ts: {c.timestamp.min()}")
print(f"  max ts: {c.timestamp.max()}")
print(f"  count: {len(c)}")
print(f"  last 5 timestamps:")
for t in c.tail(5).timestamp:
    print(f"    {t}")

events = []
with open(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data\liquidations.jsonl") as f:
    for line in f:
        if line.strip():
            events.append(json.loads(line))
print(f"\nEvents: {len(events)}")
print("  first 3:")
for e in events[:3]:
    print(f"    {e['ts']}  {e['symbol']}  {e['side']}  ${e['total_notional']:,.0f}")
print("  last 3:")
for e in events[-3:]:
    print(f"    {e['ts']}  {e['symbol']}  {e['side']}  ${e['total_notional']:,.0f}")

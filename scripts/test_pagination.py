"""Quick test: how does the funding API respond to different time windows?"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from src.exchange.hyperliquid import HyperliquidClient

c = HyperliquidClient(env="testnet")
end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

for days in [5, 20, 30, 60, 90]:
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
    df = c.get_funding_history("BTC", start_ms=start_ms, end_ms=end_ms)
    if df.empty:
        print(f"{days:3d}d: NO DATA")
    else:
        first = df["timestamp"].iloc[0]
        last = df["timestamp"].iloc[-1]
        print(f"{days:3d}d: {len(df)} rows, {first} -> {last}")

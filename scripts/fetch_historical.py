"""
HyphyLiquid — Fetch historical candles + funding for BTC and ETH.

Saves to data/ as CSVs:
- data/btc_candles_1h_30d.csv
- data/eth_candles_1h_30d.csv
- data/btc_funding_30d.csv
- data/eth_funding_30d.csv

Run:
    .\\venv\\Scripts\\python.exe scripts\\fetch_historical.py
"""

import sys
from datetime import datetime, timezone
import pandas as pd
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.exchange.hyperliquid import HyperliquidClient

DATA_DIR = PROJECT_ROOT / "data"
LOOKBACK_DAYS = int(__import__("os").environ.get("HYPERLIQUID_LOOKBACK_DAYS", "30"))
INTERVAL = "1h"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "BNB", "xyz:GOLD", "xyz:SILVER"]
ENV = __import__("os").environ.get("HYPERLIQUID_ENV", "testnet")


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    env = ENV  # HYPERLIQUID_ENV env var: 'testnet' (default) or 'mainnet'
    print(f"Connecting to {env}...")
    client = HyperliquidClient(env=env)

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - (LOOKBACK_DAYS * 24 * 60 * 60 * 1000)
    start_iso = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat()

    print(f"Window: {start_iso} -> {end_iso} ({INTERVAL} bars, {LOOKBACK_DAYS} days)")
    print()

    for symbol in SYMBOLS:
        print(f"--- {symbol} ---")

        # Candles
        df = client.get_candles(
            symbol, interval=INTERVAL, start_ms=start_ms, end_ms=end_ms
        )
        if df.empty:
            print("  candles: NO DATA")
        else:
            path = DATA_DIR / f"{symbol.lower()}_candles_{INTERVAL}_{LOOKBACK_DAYS}d_{env}.csv"
            df.to_csv(path, index=False)
            close_min = df["close"].min()
            close_max = df["close"].max()
            print(f"  candles: {len(df)} rows -> {path.name}")
            print(f"    first: {df['timestamp'].iloc[0]}")
            print(f"    last:  {df['timestamp'].iloc[-1]}")
            print(
                f"    close range: ${close_min:,.2f} — ${close_max:,.2f} "
                f"(last: ${df['close'].iloc[-1]:,.2f})"
            )

        # Funding (paginated — API caps at 500 events, returns the OLDEST 500
        # in a large range. So we walk back in 20-day chunks from "now", each
        # returning ~480 recent events, then concatenate.)
        chunk_ms = 20 * 24 * 60 * 60 * 1000
        all_funding = []
        chunk_end = end_ms
        safety_iters = 0
        while chunk_end > start_ms and safety_iters < 20:
            safety_iters += 1
            chunk_start = max(start_ms, chunk_end - chunk_ms)
            chunk = client.get_funding_history(
                symbol, start_ms=chunk_start, end_ms=chunk_end
            )
            if chunk.empty:
                break
            all_funding.append(chunk)
            # Stop if this chunk's earliest event is at or before start_ms
            if chunk["timestamp"].iloc[0] <= pd.Timestamp(
                start_ms, unit="ms", tz="UTC"
            ):
                break
            chunk_end = int(
                chunk["timestamp"].iloc[0].timestamp() * 1000
            ) - 1
        if not all_funding:
            print("  funding: NO DATA")
        else:
            funding = (
                pd.concat(all_funding)
                .drop_duplicates(subset=["timestamp"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            path = DATA_DIR / f"{symbol.lower()}_funding_{LOOKBACK_DAYS}d_{env}.csv"
            funding.to_csv(path, index=False)
            avg_rate = funding["funding_rate"].mean()
            print(f"  funding: {len(funding)} rows -> {path.name}")
            print(
                f"    range: {funding['timestamp'].iloc[0]} -> "
                f"{funding['timestamp'].iloc[-1]}"
            )
            print(f"    avg rate per hour: {avg_rate*100:.5f}%")
            print(
                f"    rate range: {funding['funding_rate'].min()*100:.5f}% - "
                f"{funding['funding_rate'].max()*100:.5f}%"
            )

    print(f"Done. Data saved to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

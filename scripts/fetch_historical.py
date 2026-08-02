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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.exchange.hyperliquid import HyperliquidClient

DATA_DIR = PROJECT_ROOT / "data"
LOOKBACK_DAYS = 30
INTERVAL = "1h"
SYMBOLS = ["BTC", "ETH"]


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    env = "testnet"  # use testnet for dev — mainnet can be added later
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
            path = DATA_DIR / f"{symbol.lower()}_candles_{INTERVAL}_{LOOKBACK_DAYS}d.csv"
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

        # Funding
        funding = client.get_funding_history(
            symbol, start_ms=start_ms, end_ms=end_ms
        )
        if funding.empty:
            print("  funding: NO DATA")
        else:
            path = DATA_DIR / f"{symbol.lower()}_funding_{LOOKBACK_DAYS}d.csv"
            funding.to_csv(path, index=False)
            avg_rate = funding["funding_rate"].mean()
            print(f"  funding: {len(funding)} rows -> {path.name}")
            print(f"    avg rate per hour: {avg_rate*100:.5f}%")
            print(
                f"    range: {funding['funding_rate'].min()*100:.5f}% — "
                f"{funding['funding_rate'].max()*100:.5f}%"
            )
        print()

    print(f"Done. Data saved to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

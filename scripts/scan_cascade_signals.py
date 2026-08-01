"""
HyphyLiquid — Cascade signal scanner

Reads the historical funding CSVs in data/ and runs the cascade
detector over them. Prints the signal timeline and summary stats.

Run:
    .\\venv\\Scripts\\python.exe scripts\\scan_cascade_signals.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.strategy.cascade import (
    detect_funding_extreme,
    summarize_funding_extremes,
)

DATA_DIR = PROJECT_ROOT / "data"
SYMBOLS = ["BTC", "ETH"]


def main() -> int:
    print("HyphyLiquid - Cascade Signal Scanner")
    print("=" * 60)
    print()

    total_signals = 0
    for symbol in SYMBOLS:
        path = DATA_DIR / f"{symbol.lower()}_funding_30d.csv"
        if not path.exists():
            print(f"[{symbol}] no data file at {path.name}, skipping")
            print()
            continue

        df = pd.read_csv(path)
        # Read-back fix: timestamps are stored as ISO strings in CSV,
        # and Hyperliquid's snapshot has mixed precision (some with .micro, some without)
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True)
        summary = summarize_funding_extremes(df)
        signals = detect_funding_extreme(df)
        total_signals += len(signals)

        print(f"[{symbol}]")
        print(f"  total funding periods: {summary['total_periods']}")
        print(
            f"  high extremes: {summary['count_high']} "
            f"(max rate: {summary['max_high']})"
        )
        print(
            f"  low extremes: {summary['count_low']} "
            f"(min rate: {summary['min_low']})"
        )
        print(f"  signals emitted: {len(signals)}")
        print()

        if signals:
            print("  First 5 signals:")
            for sig in signals[:5]:
                ts = (
                    sig.timestamp.strftime("%Y-%m-%d %H:%M")
                    if sig.timestamp is not None
                    else "?"
                )
                rate_str = (
                    f"{sig.funding_rate*100:.4f}%" if sig.funding_rate is not None else "?"
                )
                print(
                    f"    {ts}  {sig.direction.value:8s}  "
                    f"conf={sig.confidence:.2f}  rate={rate_str}"
                )
            print()

    print("=" * 60)
    print(f"Total cascade signals across all symbols: {total_signals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

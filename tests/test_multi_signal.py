"""Quick smoke test for the multi-signal detector."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.strategy.cascade import (
    detect_multi_signal_cascade,
    detect_funding_extreme,
    summarize_funding_extremes,
)

DATA_DIR = PROJECT_ROOT / "data"


def _load(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    candles = pd.read_csv(DATA_DIR / f"{symbol.lower()}_candles_1h_90d_mainnet.csv")
    candles["timestamp"] = pd.to_datetime(candles["timestamp"], format="ISO8601", utc=True)
    funding = pd.read_csv(DATA_DIR / f"{symbol.lower()}_funding_90d_mainnet.csv")
    funding["timestamp"] = pd.to_datetime(funding["timestamp"], format="ISO8601", utc=True)
    return candles, funding


def test_multi_signal_smoke() -> None:
    """Loads BTC mainnet 90d, runs both detectors, prints summary."""
    for symbol in ("BTC", "ETH"):
        candles, funding = _load(symbol)
        print(f"\n=== {symbol} ===")
        print(f"  candles: {len(candles)}, funding: {len(funding)}")

        # v1 simple
        v1_signals = detect_funding_extreme(
            funding,
            high_threshold=1.5e-5,
            low_threshold=-2.0e-5,
        )
        print(f"  v1 simple: {len(v1_signals)} signals")

        # v2 multi-signal
        v2_signals = detect_multi_signal_cascade(
            candles=candles,
            funding=funding,
            high_threshold=1.5e-5,
            low_threshold=-2.0e-5,
            funding_diff_threshold=0.0003,
            vwap_window=24,
            vwap_stretch_std=1.0,
            volume_window=24,
            volume_multiple=1.5,
        )
        print(f"  v2 multi:  {len(v2_signals)} signals")
        if v2_signals:
            # Show first 3
            for s in v2_signals[:3]:
                print(f"    {s.timestamp}  {s.direction.value:5}  conf={s.confidence:.2f}  {s.reason}")


if __name__ == "__main__":
    test_multi_signal_smoke()

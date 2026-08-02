"""Scan multi-signal thresholds to see what's reasonable."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.strategy.cascade import detect_multi_signal_cascade

DATA_DIR = PROJECT_ROOT / "data"


def _load(symbol):
    c = pd.read_csv(DATA_DIR / f"{symbol.lower()}_candles_1h_90d_mainnet.csv")
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
    f = pd.read_csv(DATA_DIR / f"{symbol.lower()}_funding_90d_mainnet.csv")
    f["timestamp"] = pd.to_datetime(f["timestamp"], format="ISO8601", utc=True)
    return c, f


# Sweep grid - vary the tightness of each condition
results = []
for high_t in (1.4e-5, 1.6e-5, 1.8e-5):
    for low_t in (-1.5e-5, -2.0e-5):
        for fdiff in (1.0e-4, 3.0e-4, 5.0e-4):
            for stretch in (0.5, 1.0, 1.5):
                for vol_mult in (1.0, 1.2, 1.5):
                    for symbol in ("BTC", "ETH"):
                        c, f = _load(symbol)
                        sigs = detect_multi_signal_cascade(
                            candles=c, funding=f,
                            high_threshold=high_t, low_threshold=low_t,
                            funding_diff_threshold=fdiff,
                            vwap_stretch_std=stretch,
                            volume_multiple=vol_mult,
                        )
                        if len(sigs) >= 5:  # only show configs with at least 5 signals
                            results.append({
                                "symbol": symbol,
                                "high_t": high_t, "low_t": low_t,
                                "fdiff": fdiff, "stretch": stretch, "vol": vol_mult,
                                "n": len(sigs),
                            })

print(f"Total configs with >=5 signals: {len(results)}")
print()
# Sort by n desc
results.sort(key=lambda x: -x["n"])
for r in results[:25]:
    print(f"  {r['symbol']}  high={r['high_t']:.1e}  low={r['low_t']:.1e}  "
          f"fdiff={r['fdiff']:.1e}  stretch={r['stretch']}  vol={r['vol']}  "
          f"n={r['n']}")

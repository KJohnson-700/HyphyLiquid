"""Debug: which condition kills the signal?"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from src.strategy.cascade import (
    _compute_vwap, _compute_volume_zscore, _compute_price_stretch,
    DEFAULT_FUNDING_EXTREME_HIGH, DEFAULT_FUNDING_EXTREME_LOW,
)

DATA_DIR = PROJECT_ROOT / "data"
c = pd.read_csv(DATA_DIR / "btc_candles_1h_90d_mainnet.csv")
c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
f = pd.read_csv(DATA_DIR / "btc_funding_90d_mainnet.csv")
f["timestamp"] = pd.to_datetime(f["timestamp"], format="ISO8601", utc=True)
f["hour_ts"] = f["timestamp"].dt.floor("h")
funding_by_hour = f.set_index("hour_ts")[["funding_rate"]].sort_index()

c = c.sort_values("timestamp").set_index("timestamp")
vwap = _compute_vwap(c, 24)
vol_ratio = _compute_volume_zscore(c, 24)
stretch = _compute_price_stretch(c, 24)
funding_diff = funding_by_hour["funding_rate"].diff()

merged = c.copy()
merged["funding_rate"] = funding_by_hour["funding_rate"].reindex(c.index, method="ffill")
merged["funding_diff"] = funding_diff.reindex(c.index, method="ffill")
merged["vwap"] = vwap
merged["vol_ratio"] = vol_ratio
merged["stretch"] = stretch
ready = merged.dropna(subset=["funding_rate", "funding_diff", "vwap", "vol_ratio", "stretch"])

print(f"Bars with all 4 indicators ready: {len(ready)}")
print()

# Count how many bars pass each condition
n = len(ready)
c1 = (ready["funding_rate"] >= 1.4e-5) | (ready["funding_rate"] <= -1.5e-5)
c2 = ready["funding_diff"].abs() >= 1e-4
c3 = ready["stretch"].abs() >= 0.5
c4 = ready["vol_ratio"] >= 1.0
print(f"  cond1 (funding extreme):        {c1.sum()} / {n}  ({c1.sum()/n*100:.1f}%)")
print(f"  cond2 (funding diff > 0.01%):  {c2.sum()} / {n}  ({c2.sum()/n*100:.1f}%)")
print(f"  cond3 (stretch > 0.5 stdev):   {c3.sum()} / {n}  ({c3.sum()/n*100:.1f}%)")
print(f"  cond4 (vol > 1.0x avg):        {c4.sum()} / {n}  ({c4.sum()/n*100:.1f}%)")
print(f"  ALL FOUR:                      {(c1 & c2 & c3 & c4).sum()}")
print()

# Distribution of each
print("Funding rate distribution on ready bars:")
print(ready["funding_rate"].describe())
print()
print("Funding diff distribution on ready bars:")
print(ready["funding_diff"].abs().describe())
print()
print("Stretch distribution on ready bars:")
print(ready["stretch"].abs().describe())
print()
print("Vol ratio distribution on ready bars:")
print(ready["vol_ratio"].describe())

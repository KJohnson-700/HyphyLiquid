"""Debug script: print what _find_data is checking."""
import sys
from pathlib import Path

DATA_DIR = Path(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data")

for env in ("mainnet", "testnet", ""):
    for suffix in ("90d", "30d", "7d"):
        c = (
            DATA_DIR / f"btc_candles_1h_{suffix}d_{env}.csv"
            if env
            else DATA_DIR / f"btc_candles_1h_{suffix}d.csv"
        )
        f = (
            DATA_DIR / f"btc_funding_{suffix}d_{env}.csv"
            if env
            else DATA_DIR / f"btc_funding_{suffix}d.csv"
        )
        print(f"env={env!r:12} suffix={suffix!r:6}  c={c.name}  c_exists={c.exists()}  f_exists={f.exists()}")

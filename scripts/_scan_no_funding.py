"""Scan price-stretch + volume (no funding) cascade detectors."""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
from src.strategy.cascade import _compute_volume_zscore, _compute_price_stretch

DATA_DIR = PROJECT_ROOT / "data"


def _load(symbol):
    c = pd.read_csv(DATA_DIR / f"{symbol.lower()}_candles_1h_90d_mainnet.csv")
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
    return c


def detect_momentum_cascade(
    candles: pd.DataFrame,
    move_stdev: float = 2.0,    # 1h return must be > N stdev
    vol_multiple: float = 2.0,  # volume must be > N x avg
    revert_direction: bool = True,  # short after big down move, long after big up
    return_window: int = 24,    # 24h window for stdev
    vol_window: int = 24,
) -> pd.DataFrame:
    """
    Pure momentum cascade: detect big 1h moves with elevated volume.
    Entry direction is the FADE (against the move).
    """
    c = candles.sort_values("timestamp").set_index("timestamp").copy()
    c["ret_1h"] = c["close"].pct_change()
    c["ret_stdev"] = c["ret_1h"].rolling(return_window, min_periods=return_window).std()
    c["z_move"] = c["ret_1h"] / c["ret_stdev"]
    c["vol_ratio"] = _compute_volume_zscore(c, vol_window)
    c = c.dropna(subset=["z_move", "vol_ratio"])

    c["abs_z"] = c["z_move"].abs()
    big_moves = c[(c["abs_z"] >= move_stdev) & (c["vol_ratio"] >= vol_multiple)].copy()
    if revert_direction:
        big_moves["direction"] = np.where(big_moves["z_move"] < 0, "long", "short")
    else:
        big_moves["direction"] = np.where(big_moves["z_move"] < 0, "short", "long")
    return big_moves


print("=== Momentum cascade detector (no funding) ===\n")
for symbol in ("BTC", "ETH"):
    c = _load(symbol)
    print(f"--- {symbol} ---")
    print(f"  {'move_st':>8}  {'vol':>5}  {'trades':>7}  {'long':>5}  {'short':>5}  description")
    for move in (1.0, 1.5, 2.0, 2.5, 3.0):
        for vol in (1.0, 1.5, 2.0, 2.5, 3.0):
            sigs = detect_momentum_cascade(c, move_stdev=move, vol_multiple=vol, revert_direction=True)
            n_long = (sigs["direction"] == "long").sum()
            n_short = (sigs["direction"] == "short").sum()
            print(f"  {move:>8.1f}  {vol:>5.1f}  {len(sigs):>7}  {n_long:>5}  {n_short:>5}")
    print()

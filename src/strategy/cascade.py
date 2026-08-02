"""
HyphyLiquid — BTC/ETH cascade counter-trade strategy

Detects extreme funding rate events and emits counter-trade signals.
Pure signal logic — every signal still goes through src/risk.py
before reaching the order manager.

Direction logic:
  - High positive funding (longs pay shorts) = over-leveraged longs
    -> SHORT signal (expect deleveraging move down)
  - Low/negative funding (shorts pay longs) = over-leveraged shorts
    -> LONG signal (expect squeeze up)

Confidence scales with how extreme the rate is beyond the threshold,
capped at 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import pandas as pd


class SignalDirection(Enum):
    LONG = "long"
    SHORT = "short"
    NO_TRADE = "no_trade"


@dataclass
class CascadeSignal:
    """A single counter-trade signal. Risk module decides if we act on it."""

    symbol: str
    direction: SignalDirection
    confidence: float  # 0.0 to 1.0
    reason: str
    funding_rate: Optional[float] = None
    timestamp: Optional[pd.Timestamp] = None


# Defaults — overridable per call
# IMPORTANT: Hyperliquid funding is HOURLY (not 8h as on CEXes).
# These thresholds are calibrated against the per-hour funding rate
# returned by info.funding_history(). A value of 0.001 = 0.10% per hour,
# which equals ~0.80% per 8h-equivalent — quite extreme.
DEFAULT_FUNDING_EXTREME_HIGH = 0.0010   # 0.10% per hour
DEFAULT_FUNDING_EXTREME_LOW = -0.0005    # -0.05% per hour
DEFAULT_CONFIDENCE_FLOOR = 0.3
DEFAULT_CONFIDENCE_SLOPE = 5.0  # how fast confidence grows past threshold


def detect_funding_extreme(
    funding_df: pd.DataFrame,
    high_threshold: float = DEFAULT_FUNDING_EXTREME_HIGH,
    low_threshold: float = DEFAULT_FUNDING_EXTREME_LOW,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    confidence_slope: float = DEFAULT_CONFIDENCE_SLOPE,
) -> List[CascadeSignal]:
    """
    Scan funding history for extreme rates that suggest over-leveraged
    one-sided positioning.

    Args:
        funding_df: DataFrame with columns timestamp, coin, funding_rate, premium
        high_threshold: rate at/above which longs are over-leveraged (SHORT signal)
        low_threshold: rate at/below which shorts are over-leveraged (LONG signal)
        confidence_floor: minimum confidence to emit a signal
        confidence_slope: how fast confidence grows past threshold (rate per 1% overage)

    Returns:
        List of CascadeSignal objects, one per extreme event.
    """
    if funding_df.empty:
        return []

    signals: List[CascadeSignal] = []
    for _, row in funding_df.iterrows():
        rate = float(row["funding_rate"])
        ts = row["timestamp"]
        coin = row.get("coin", "BTC")

        if rate >= high_threshold:
            excess = rate - high_threshold
            confidence = min(1.0, confidence_floor + excess * confidence_slope * 100)
            signals.append(
                CascadeSignal(
                    symbol=coin,
                    direction=SignalDirection.SHORT,
                    confidence=confidence,
                    reason=(
                        f"funding extreme HIGH: {rate*100:.4f}% per hour "
                        f"(threshold {high_threshold*100:.2f}%/hr)"
                    ),
                    funding_rate=rate,
                    timestamp=ts,
                )
            )
        elif rate <= low_threshold:
            excess = abs(rate - low_threshold)
            confidence = min(1.0, confidence_floor + excess * confidence_slope * 100)
            signals.append(
                CascadeSignal(
                    symbol=coin,
                    direction=SignalDirection.LONG,
                    confidence=confidence,
                    reason=(
                        f"funding extreme LOW: {rate*100:.4f}% per hour "
                        f"(threshold {low_threshold*100:.2f}%/hr)"
                    ),
                    funding_rate=rate,
                    timestamp=ts,
                )
            )
    return signals


def summarize_funding_extremes(
    funding_df: pd.DataFrame,
    high_threshold: float = DEFAULT_FUNDING_EXTREME_HIGH,
    low_threshold: float = DEFAULT_FUNDING_EXTREME_LOW,
) -> dict:
    """Count and quantify funding extremes in a history."""
    if funding_df.empty:
        return {
            "count_high": 0,
            "count_low": 0,
            "max_high": None,
            "min_low": None,
            "total_periods": 0,
        }

    high = funding_df[funding_df["funding_rate"] >= high_threshold]
    low = funding_df[funding_df["funding_rate"] <= low_threshold]
    return {
        "count_high": int(len(high)),
        "count_low": int(len(low)),
        "max_high": float(high["funding_rate"].max()) if not high.empty else None,
        "min_low": float(low["funding_rate"].min()) if not low.empty else None,
        "total_periods": int(len(funding_df)),
    }


# ---- Multi-signal cascade detector (v2) ----
#
# Simple funding-extreme alone is structurally wrong on real mainnet data
# (verified 2026-08-01: 32% configs profitable, CoV 1.55). v2 requires
# MULTIPLE conditions to align before emitting a signal.
#
# Inputs: 1h candles + hourly funding.  All four conditions below must hold
# at the same bar to emit a signal.
#
# Conditions:
#   1. Funding is extreme (longs paying OR shorts paying at threshold).
#   2. Funding rate-of-change is high (positioning is SHIFTING, not steady-state).
#   3. Price is stretched from rolling VWAP (>= 1 stdev).
#   4. Volume is elevated (>= 1.5x rolling avg) - the cascade trigger.
#
# Why this should work: the simple funding-level test fires on every quiet
# hour of steady-state premium. The multi-signal version only fires when
# something is actually HAPPENING (volume + price stretch + positioning shift).

DEFAULT_VWAP_WINDOW = 24           # bars (24h)
DEFAULT_VOLUME_WINDOW = 24         # bars (24h)
DEFAULT_FUNDING_DIFF_THRESHOLD = 0.0003  # 0.03% per hour change
DEFAULT_VWAP_STRETCH_STD = 1.0     # 1 standard deviation
DEFAULT_VOLUME_MULTIPLE = 1.5      # 1.5x average


def _compute_vwap(candles: pd.DataFrame, window: int) -> pd.Series:
    """Rolling VWAP: sum(price * volume) / sum(volume) over `window` bars.
    Uses typical price (H+L+C)/3 as the price proxy."""
    if "high" in candles.columns and "low" in candles.columns:
        tp = (candles["high"] + candles["low"] + candles["close"]) / 3.0
    else:
        tp = candles["close"]
    pv = tp * candles["volume"]
    return pv.rolling(window, min_periods=window).sum() / candles["volume"].rolling(window, min_periods=window).sum()


def _compute_volume_zscore(candles: pd.DataFrame, window: int) -> pd.Series:
    """Z-score of current volume vs rolling mean (no std to keep it simple)."""
    avg = candles["volume"].rolling(window, min_periods=window).mean()
    return candles["volume"] / avg


def _compute_price_stretch(candles: pd.DataFrame, window: int) -> pd.Series:
    """Distance from rolling mean in stdev units."""
    mean = candles["close"].rolling(window, min_periods=window).mean()
    std = candles["close"].rolling(window, min_periods=window).std()
    return (candles["close"] - mean) / std


def detect_multi_signal_cascade(
    candles: pd.DataFrame,
    funding: pd.DataFrame,
    high_threshold: float = DEFAULT_FUNDING_EXTREME_HIGH,
    low_threshold: float = DEFAULT_FUNDING_EXTREME_LOW,
    funding_diff_threshold: float = DEFAULT_FUNDING_DIFF_THRESHOLD,
    vwap_window: int = DEFAULT_VWAP_WINDOW,
    vwap_stretch_std: float = DEFAULT_VWAP_STRETCH_STD,
    volume_window: int = DEFAULT_VOLUME_WINDOW,
    volume_multiple: float = DEFAULT_VOLUME_MULTIPLE,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> List[CascadeSignal]:
    """
    Multi-signal cascade detector. Emits a signal only when 4 conditions align
    at the same bar: funding extreme + funding shifting + price stretched
    from VWAP + volume elevated.

    Returns CascadeSignal objects compatible with the existing backtest.
    """
    if candles.empty or funding.empty:
        return []

    # Align funding timestamps to hour (same fix as the directional check)
    f = funding.copy()
    f["timestamp"] = pd.to_datetime(f["timestamp"], format="ISO8601", utc=True)
    f["hour_ts"] = f["timestamp"].dt.floor("h")
    funding_by_hour = f.set_index("hour_ts")[["funding_rate"]].sort_index()

    c = candles.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
    c = c.sort_values("timestamp").set_index("timestamp")

    # Compute derived series
    vwap = _compute_vwap(c, vwap_window)
    vol_ratio = _compute_volume_zscore(c, volume_window)
    stretch = _compute_price_stretch(c, vwap_window)
    funding_diff = funding_by_hour["funding_rate"].diff()

    # Merge: for each candle bar, get the funding rate that JUST occurred (at hour start)
    # and its previous value
    merged = c.copy()
    merged["funding_rate"] = funding_by_hour["funding_rate"].reindex(c.index, method="ffill")
    merged["funding_diff"] = funding_diff.reindex(c.index, method="ffill")
    merged["vwap"] = vwap
    merged["vol_ratio"] = vol_ratio
    merged["stretch"] = stretch

    # Drop bars where any condition can't be evaluated (warmup)
    ready = merged.dropna(subset=["funding_rate", "funding_diff", "vwap", "vol_ratio", "stretch"])

    signals: List[CascadeSignal] = []
    for ts, row in ready.iterrows():
        rate = float(row["funding_rate"])
        fdiff = float(row["funding_diff"])
        vwap_v = float(row["vwap"])
        vr = float(row["vol_ratio"])
        st = float(row["stretch"])

        # Determine direction from funding level
        if rate >= high_threshold:
            direction = SignalDirection.SHORT
            level_excess = rate - high_threshold
        elif rate <= low_threshold:
            direction = SignalDirection.LONG
            level_excess = abs(rate - low_threshold)
        else:
            continue  # no funding extreme -> no signal

        # Check the other 3 conditions
        # 1. Funding is shifting (rate of change)
        if abs(fdiff) < funding_diff_threshold:
            continue
        # 2. Price stretched from VWAP (in stdev units)
        if abs(st) < vwap_stretch_std:
            continue
        # 3. Volume elevated
        if vr < volume_multiple:
            continue

        # Direction must align with stretch (short when stretched above vwap, long when below)
        if direction == SignalDirection.SHORT and st < 0:
            continue  # funding says short but price is below vwap - counter-trend, skip
        if direction == SignalDirection.LONG and st > 0:
            continue  # funding says long but price is above vwap - skip

        # Confidence: combine magnitude of all signals
        conf = min(
            1.0,
            confidence_floor
            + level_excess * confidence_slope * 100
            + abs(fdiff) * 1000  # 0.001 diff = 1.0 confidence
            + max(0, abs(st) - vwap_stretch_std) * 0.2
            + max(0, vr - volume_multiple) * 0.1,
        )

        coin = row.get("coin", "BTC")
        signals.append(
            CascadeSignal(
                symbol=str(coin),
                direction=direction,
                confidence=conf,
                reason=(
                    f"multi-signal: funding={rate*100:.4f}% (diff {fdiff*100:+.4f}%), "
                    f"stretch={st:+.2f}stdev, vol={vr:.2f}x"
                ),
                funding_rate=rate,
                timestamp=pd.Timestamp(ts),
            )
        )
    return signals


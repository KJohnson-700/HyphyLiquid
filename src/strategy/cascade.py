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
DEFAULT_FUNDING_EXTREME_HIGH = 0.0010   # 0.10% per 8h
DEFAULT_FUNDING_EXTREME_LOW = -0.0005    # -0.05% per 8h
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
                        f"funding extreme HIGH: {rate*100:.4f}% per 8h "
                        f"(threshold {high_threshold*100:.2f}%)"
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
                        f"funding extreme LOW: {rate*100:.4f}% per 8h "
                        f"(threshold {low_threshold*100:.2f}%)"
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

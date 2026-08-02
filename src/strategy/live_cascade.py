"""
HyphyLiquid - Live cascade detector.

Combines a current HyperPerps snapshot with current HL price data to
identify a "cascade setup" - a state where forced liquidations are
likely to fire soon and a fade entry is justified.

Input: a HyperPerps snapshot dict (from /api/public/heatmap/{symbol})
       and a current price (from Hyperliquid candles/allMids)

Output: a LiveCascadeState describing the setup + suggested direction
        if any, or NO_SETUP if conditions don't warrant a trade.

Logic (v1 - conservative, mostly see if anything fires):
  A cascade setup requires ALL of:
    1. trapped_pct.{side}_underwater_5pct > 0.30 (meaningful trapped crowd)
    2. forced_liq_proximity.{side}_near_liq_2pct > 0.02 (real proximity)
    3. cascade_mass.{opposite_side}.within_2pct > $5M (fuel to cascade)
    4. fresh_money.net_bias_24h SIGNIFICANTLY OPPOSITE trapped side
       (new money is NOT bailing the trapped side out)
    5. current price within 1-3% of nearest cluster (proximity trigger)
  Then the side with high trapped% is the side that will get squeezed;
  we FADE the cascade by entering the opposite direction.

  If trapped longs and trapped shorts both high -> no clear direction -> skip.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LiveSignal(Enum):
    NO_SETUP = "no_setup"
    LONG = "long"     # fade a short cascade (shorts about to be squeezed)
    SHORT = "short"   # fade a long cascade (longs about to be squeezed)


@dataclass
class LiveCascadeState:
    symbol: str
    signal: LiveSignal
    confidence: float  # 0.0-1.0
    spot_at_compute: float
    current_price: float
    cascade_distance_pct: float  # current_price vs spot_at_compute
    trapped_long_pct: float      # from snapshot
    trapped_short_pct: float
    liq_within_2pct_long: float
    liq_within_2pct_short: float
    fresh_money_24h: float
    reason: str


# Defaults
DEFAULT_TRAPPED_THRESHOLD = 0.30      # 30% of a side trapped >= 5% underwater
DEFAULT_PROXIMITY_THRESHOLD = 0.02   # 2% of side within 2% of liquidation
DEFAULT_FUEL_USD = 5_000_000.0       # $5M cascade fuel within 2% (opposite side)
DEFAULT_FRESH_MONEY_OPPOSITE = 0.10  # fresh money must be 0.10+ opposite trapped side


def detect_live_cascade(
    symbol: str,
    snapshot: dict,
    current_price: Optional[float] = None,
    trapped_threshold: float = DEFAULT_TRAPPED_THRESHOLD,
    proximity_threshold: float = DEFAULT_PROXIMITY_THRESHOLD,
    fuel_usd: float = DEFAULT_FUEL_USD,
    fresh_money_opposite: float = DEFAULT_FRESH_MONEY_OPPOSITE,
) -> LiveCascadeState:
    """
    Inspect a HyperPerps snapshot + current price for a cascade setup.

    Returns a LiveCascadeState with the suggested signal and the supporting
    numbers. NO_SETUP if conditions don't warrant a trade.
    """
    spot = float(snapshot.get("spot_at_compute", 0.0))
    price = float(current_price) if current_price is not None else spot
    distance_pct = ((price - spot) / spot * 100) if spot else 0.0

    # Extract key fields
    trapped = snapshot.get("trapped_pct", {})
    proximity = snapshot.get("forced_liq_proximity", {})
    cascade = snapshot.get("cascade_mass", {})
    fresh = snapshot.get("fresh_money", {})
    crowd = snapshot.get("crowd_leverage", {})

    long_trapped = float(trapped.get("longs_underwater_5pct", 0.0))
    short_trapped = float(trapped.get("shorts_underwater_5pct", 0.0))
    long_near = float(proximity.get("longs_near_liq_2pct", 0.0))
    short_near = float(proximity.get("shorts_near_liq_2pct", 0.0))
    long_fuel = float(cascade.get("long", {}).get("within_2pct", 0.0))
    short_fuel = float(cascade.get("short", {}).get("within_2pct", 0.0))
    fresh_24h = float(fresh.get("net_bias_24h", 0.0))
    long_lev = float(crowd.get("long_avg", 0.0))
    short_lev = float(crowd.get("short_avg", 0.0))

    # Determine if either side is in a setup
    long_setup = (
        long_trapped >= trapped_threshold
        and long_near >= proximity_threshold
        and short_fuel >= fuel_usd          # shorts have fuel to cascade the longs
        and fresh_24h <= -fresh_money_opposite  # fresh money leaning short (opposite of long trapped)
    )
    short_setup = (
        short_trapped >= trapped_threshold
        and short_near >= proximity_threshold
        and long_fuel >= fuel_usd
        and fresh_24h >= fresh_money_opposite   # fresh money leaning long (opposite of short trapped)
    )

    # If both sides are setup, no clear direction
    if long_setup and short_setup:
        return LiveCascadeState(
            symbol=symbol, signal=LiveSignal.NO_SETUP, confidence=0.0,
            spot_at_compute=spot, current_price=price,
            cascade_distance_pct=distance_pct,
            trapped_long_pct=long_trapped, trapped_short_pct=short_trapped,
            liq_within_2pct_long=long_near, liq_within_2pct_short=short_near,
            fresh_money_24h=fresh_24h,
            reason="both sides set up: no clear direction",
        )
    if long_setup:
        # Longs are trapped + near liq + shorts have fuel + fresh money is short-biased
        # -> fade the long cascade by going SHORT
        confidence = min(1.0, long_trapped + long_near * 5 + (short_fuel / 1e7))
        return LiveCascadeState(
            symbol=symbol, signal=LiveSignal.SHORT, confidence=confidence,
            spot_at_compute=spot, current_price=price,
            cascade_distance_pct=distance_pct,
            trapped_long_pct=long_trapped, trapped_short_pct=short_trapped,
            liq_within_2pct_long=long_near, liq_within_2pct_short=short_near,
            fresh_money_24h=fresh_24h,
            reason=(f"long setup: trapped={long_trapped:.0%} near_liq={long_near:.0%} "
                    f"short_fuel=${short_fuel/1e6:.1f}M fresh_24h={fresh_24h:+.2f} "
                    f"crowd_lev long={long_lev:.0f}x short={short_lev:.0f}x"),
        )
    if short_setup:
        # Shorts are trapped + near liq + longs have fuel + fresh money is long-biased
        # -> fade the short cascade by going LONG
        confidence = min(1.0, short_trapped + short_near * 5 + (long_fuel / 1e7))
        return LiveCascadeState(
            symbol=symbol, signal=LiveSignal.LONG, confidence=confidence,
            spot_at_compute=spot, current_price=price,
            cascade_distance_pct=distance_pct,
            trapped_long_pct=long_trapped, trapped_short_pct=short_trapped,
            liq_within_2pct_long=long_near, liq_within_2pct_short=short_near,
            fresh_money_24h=fresh_24h,
            reason=(f"short setup: trapped={short_trapped:.0%} near_liq={short_near:.0%} "
                    f"long_fuel=${long_fuel/1e6:.1f}M fresh_24h={fresh_24h:+.2f} "
                    f"crowd_lev long={long_lev:.0f}x short={short_lev:.0f}x"),
        )

    # No setup - explain why
    reasons = []
    if long_trapped < trapped_threshold and short_trapped < trapped_threshold:
        reasons.append(f"both trapped low (L={long_trapped:.0%}, S={short_trapped:.0%})")
    if long_near < proximity_threshold and short_near < proximity_threshold:
        reasons.append("no proximity to liq")
    if long_fuel < fuel_usd and short_fuel < fuel_usd:
        reasons.append("no cascade fuel")
    if abs(fresh_24h) < fresh_money_opposite:
        reasons.append(f"fresh money not decisive ({fresh_24h:+.2f})")
    return LiveCascadeState(
        symbol=symbol, signal=LiveSignal.NO_SETUP, confidence=0.0,
        spot_at_compute=spot, current_price=price,
        cascade_distance_pct=distance_pct,
        trapped_long_pct=long_trapped, trapped_short_pct=short_trapped,
        liq_within_2pct_long=long_near, liq_within_2pct_short=short_near,
        fresh_money_24h=fresh_24h,
        reason="; ".join(reasons) if reasons else "no clear setup",
    )

"""
HyphyLiquid - Liquidation detector for the public HL trade feed.

Given a stream of public trades, identify "burst" events that look like
forced liquidations: large same-direction fills in a tight time window,
or single very-large trades.

Heuristics (any one triggers a "probable_liquidation" flag):
  1. Total burst notional > $1M in <2 seconds, same direction
  2. Single trade notional > $500K
  3. Burst with decreasing-size fills at constant price (classic liquidation signature)

This is a probabilistic detector, not a guaranteed one. After running
for a few days we can correlate with subsequent price action to see if
the detector actually predicts post-cascade mean reversion.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class TradeEvent:
    """A single normalized trade."""
    symbol: str
    timestamp_ms: int
    side: str  # "A" (ask/sell) or "B" (bid/buy)
    price: float
    size: float
    tid: Optional[int] = None

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class LiquidationEvent:
    """A probable liquidation event, with one or more fills."""
    symbol: str
    timestamp_ms: int
    side: str
    total_notional: float
    n_fills: int
    price_avg: float
    duration_ms: int
    confidence: float  # 0-1
    reason: str
    fills: List[TradeEvent] = field(default_factory=list)


# Thresholds (tunable)
SINGLE_TRADE_MIN_NOTIONAL = 500_000.0   # $500k single fill = likely liquidation
BURST_TOTAL_NOTIONAL = 1_000_000.0      # $1M in <2s same direction = likely liquidation
BURST_MAX_DURATION_MS = 2_000           # 2 second window
PRICE_CONSTANCY_PCT = 0.001             # fills within 0.1% of each other = "constant price"

# Per-symbol thresholds. Alts have smaller per-trade notionals, so the
# BTC/ETH defaults would never fire on them. Calibrated to the asset's
# typical trade size; revisit if a specific alt is under/over-firing.
DEFAULT_THRESHOLDS = {
    "single_trade_min": SINGLE_TRADE_MIN_NOTIONAL,
    "burst_total_min": BURST_TOTAL_NOTIONAL,
    "burst_window_ms": BURST_MAX_DURATION_MS,
    "price_constancy_pct": PRICE_CONSTANCY_PCT,
}

PER_SYMBOL_THRESHOLDS: dict[str, dict] = {
    # BTC: use defaults (500k / 1M)
    "ETH":  {"single_trade_min": 250_000.0, "burst_total_min": 500_000.0},
    "SOL":  {"single_trade_min": 100_000.0, "burst_total_min": 250_000.0},
    "HYPE": {"single_trade_min": 100_000.0, "burst_total_min": 250_000.0},
    "DOGE": {"single_trade_min": 50_000.0,  "burst_total_min": 150_000.0},
    "BNB":  {"single_trade_min": 100_000.0, "burst_total_min": 250_000.0},
    # BTC: explicit so it shows up in the table
    "BTC":  {"single_trade_min": 500_000.0, "burst_total_min": 1_000_000.0},
}


def thresholds_for(symbol: str) -> dict:
    """Return the threshold dict for a symbol, falling back to defaults."""
    return {**DEFAULT_THRESHOLDS, **PER_SYMBOL_THRESHOLDS.get(symbol, {})}


class LiquidationDetector:
    """Stateful detector: feed trades, get liquidation events out.

    Thresholds default to BTC's. Pass per_symbol=True in __init__ to
    use PER_SYMBOL_THRESHOLDS (the detector then picks thresholds per
    trade based on trade.symbol)."""

    def __init__(
        self,
        single_trade_min: float = SINGLE_TRADE_MIN_NOTIONAL,
        burst_total_min: float = BURST_TOTAL_NOTIONAL,
        burst_window_ms: int = BURST_MAX_DURATION_MS,
        price_constancy_pct: float = PRICE_CONSTANCY_PCT,
        per_symbol: bool = False,
    ):
        self.single_trade_min = single_trade_min
        self.burst_total_min = burst_total_min
        self.burst_window_ms = burst_window_ms
        self.price_constancy_pct = price_constancy_pct
        self.per_symbol = per_symbol
        # Per-symbol rolling window of recent trades (for burst detection)
        self._recent: dict[str, List[TradeEvent]] = defaultdict(list)
        self.events: List[LiquidationEvent] = []

    def _thresholds(self, symbol: str) -> tuple[float, float, int, float]:
        if self.per_symbol:
            t = thresholds_for(symbol)
            return (
                t["single_trade_min"],
                t["burst_total_min"],
                t["burst_window_ms"],
                t["price_constancy_pct"],
            )
        return (
            self.single_trade_min,
            self.burst_total_min,
            self.burst_window_ms,
            self.price_constancy_pct,
        )

    def feed(self, trade: TradeEvent) -> List[LiquidationEvent]:
        """Feed a single trade; return any liquidation events detected."""
        new_events: List[LiquidationEvent] = []
        sym = trade.symbol
        single_trade_min, burst_total_min, burst_window_ms, price_constancy_pct = (
            self._thresholds(sym)
        )
        recent = self._recent[sym]

        # Drop trades older than the burst window (per-symbol)
        cutoff = trade.timestamp_ms - burst_window_ms
        self._recent[sym] = [t for t in recent if t.timestamp_ms >= cutoff]
        self._recent[sym].append(trade)
        recent = self._recent[sym]

        # Check 1: Single very large trade (per-symbol threshold)
        if trade.notional >= single_trade_min:
            ev = LiquidationEvent(
                symbol=sym,
                timestamp_ms=trade.timestamp_ms,
                side=trade.side,
                total_notional=trade.notional,
                n_fills=1,
                price_avg=trade.price,
                duration_ms=0,
                confidence=0.85,
                reason=f"single large trade: ${trade.notional:,.0f}",
                fills=[trade],
            )
            self.events.append(ev)
            new_events.append(ev)

        # Check 2: Burst - aggregate same-direction trades in window
        for side in ("A", "B"):
            same_dir = [t for t in recent if t.side == side]
            if len(same_dir) < 2:
                continue
            total = sum(t.notional for t in same_dir)
            if total < burst_total_min:
                continue
            prices = [t.price for t in same_dir]
            avg_p = sum(prices) / len(prices)
            price_range = (max(prices) - min(prices)) / avg_p if avg_p else 0
            # Check 3: Decreasing size (liquidation eating through the book)
            sizes = [t.size for t in sorted(same_dir, key=lambda x: x.timestamp_ms)]
            decreasing = all(
                sizes[i] >= sizes[i+1] * 0.7  # each next size within 70% of prior
                for i in range(len(sizes) - 1)
            ) if len(sizes) >= 2 else False
            if price_range <= price_constancy_pct and decreasing and len(same_dir) >= 3:
                ev = LiquidationEvent(
                    symbol=sym,
                    timestamp_ms=trade.timestamp_ms,
                    side=side,
                    total_notional=total,
                    n_fills=len(same_dir),
                    price_avg=avg_p,
                    duration_ms=same_dir[-1].timestamp_ms - same_dir[0].timestamp_ms,
                    confidence=0.90,
                    reason=(f"decreasing-size burst: {len(same_dir)} fills, "
                            f"${total:,.0f}, price range {price_range*100:.3f}%"),
                    fills=list(same_dir),
                )
                self.events.append(ev)
                new_events.append(ev)
                # Clear window so we don't re-detect same burst
                self._recent[sym] = []
            elif price_range <= price_constancy_pct and len(same_dir) >= 5:
                # Same-price burst without decreasing pattern
                ev = LiquidationEvent(
                    symbol=sym,
                    timestamp_ms=trade.timestamp_ms,
                    side=side,
                    total_notional=total,
                    n_fills=len(same_dir),
                    price_avg=avg_p,
                    duration_ms=same_dir[-1].timestamp_ms - same_dir[0].timestamp_ms,
                    confidence=0.70,
                    reason=f"same-price burst: {len(same_dir)} fills, ${total:,.0f}",
                    fills=list(same_dir),
                )
                self.events.append(ev)
                new_events.append(ev)
                self._recent[sym] = []
        return new_events

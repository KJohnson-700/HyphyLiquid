"""Tests for the liquidation detector."""
import pytest

from src.strategy.liquidation_detector import (
    LiquidationDetector,
    TradeEvent,
)


def _t(sym: str, t: int, side: str, price: float, size: float, tid: int = 0) -> TradeEvent:
    return TradeEvent(symbol=sym, timestamp_ms=t, side=side, price=price, size=size, tid=tid)


def test_single_large_trade_detected() -> None:
    d = LiquidationDetector()
    events = d.feed(_t("BTC", 1000, "A", 60000, 10.0, tid=1))
    assert len(events) == 1
    assert events[0].n_fills == 1
    assert events[0].total_notional == 600_000
    assert events[0].confidence >= 0.8


def test_burst_same_price_detected() -> None:
    d = LiquidationDetector(burst_total_min=1_000_000, single_trade_min=500_000)
    base = 1000
    events = []
    # 5 sells at same price totaling >$1M in 500ms
    for i, sz in enumerate([200, 150, 100, 80, 70]):
        events.extend(d.feed(_t("ETH", base + i * 100, "A", 1800, sz, tid=i)))
    # Last one should detect the burst
    detected = [e for e in events if e.n_fills >= 2]
    assert len(detected) >= 1
    assert detected[-1].n_fills >= 3


def test_random_small_trades_not_detected() -> None:
    d = LiquidationDetector()
    events = []
    for i in range(10):
        events.extend(d.feed(_t("BTC", 1000 + i * 100, "A", 60000, 0.1)))
    assert all(e.n_fills == 0 for e in events if not e.n_fills)
    # No big burst
    big = [e for e in events if e.total_notional > 500_000]
    assert len(big) == 0


def test_opposite_directions_dont_combine() -> None:
    d = LiquidationDetector()
    # Mixed buys and sells at same price - shouldn't combine into a burst
    events = []
    for i, side in enumerate(["A", "B", "A", "B", "A"]):
        events.extend(d.feed(_t("ETH", 1000 + i * 50, side, 1800, 5, tid=i)))
    big = [e for e in events if e.total_notional > 500_000]
    assert len(big) == 0


def test_window_expires() -> None:
    d = LiquidationDetector()
    events = []
    # One big sell, then 3s later another big sell - separate events
    events.extend(d.feed(_t("BTC", 1000, "A", 60000, 10)))
    events.extend(d.feed(_t("BTC", 4000, "A", 60000, 10)))
    # The first was a single large trade. The second is a separate single trade.
    assert len(events) == 2
    assert all(e.n_fills == 1 for e in events)


def test_high_confidence_decreasing_burst() -> None:
    d = LiquidationDetector(burst_total_min=1_000_000, single_trade_min=500_000)
    events = []
    # Decreasing sizes at constant price, total >$1M
    for i, sz in enumerate([300, 200, 150, 100, 50, 30]):
        events.extend(d.feed(_t("ETH", 1000 + i * 50, "A", 1800, sz, tid=i)))
    detected = [e for e in events if e.confidence >= 0.85 and e.n_fills >= 3]
    assert len(detected) >= 1
    assert detected[-1].reason.startswith("decreasing-size burst")

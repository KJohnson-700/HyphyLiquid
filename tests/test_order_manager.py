"""Tests for the order manager sizing/validation logic.

These tests DO NOT call the exchange (no real orders placed).
They cover: ATR calc, size calc, tick rounding, risk rejection paths.
"""
from pathlib import Path

import pandas as pd
import pytest

from src.execution.order_manager import OrderManager, OrderResult
from src.risk import RiskVerdict
from src.strategy.cascade import CascadeSignal, SignalDirection

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _FakeExchange:
    """Captures bulk_orders calls without hitting the network."""
    def __init__(self, response: dict | None = None, raise_exc: Exception | None = None):
        self.calls = []
        self.response = response or {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"resting": {"oid": 100}},
                        {"resting": {"oid": 101}},
                        {"resting": {"oid": 102}},
                    ]
                },
            },
        }
        self.raise_exc = raise_exc

    def bulk_orders(self, requests, grouping=None):
        self.calls.append((requests, grouping))
        if self.raise_exc:
            raise self.raise_exc
        return self.response


class _FakeInfo:
    pass


def _candles(n: int = 30, base: float = 60000.0, vol: float = 200.0) -> pd.DataFrame:
    """Make a deterministic 1h OHLCV series."""
    import numpy as np
    rng = np.random.default_rng(42)
    closes = base + rng.normal(0, vol, n).cumsum()
    highs = closes + rng.uniform(50, 200, n)
    lows = closes - rng.uniform(50, 200, n)
    opens = closes + rng.normal(0, 30, n)
    times = pd.date_range("2026-08-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": times, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": rng.uniform(100, 1000, n),
    })


def _short_signal(symbol: str = "BTC", confidence: float = 0.7) -> CascadeSignal:
    return CascadeSignal(
        symbol=symbol, timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
        direction=SignalDirection.SHORT, confidence=confidence,
        reason="test", funding_rate=0.0,
    )


def test_short_sizing_respects_1pct_risk() -> None:
    mgr = OrderManager(_FakeExchange(), _FakeInfo(), bankroll=1000.0)
    candles = _candles()
    result = mgr.execute(_short_signal(), candles, current_price=60000.0)
    # 1% of $1000 = $10 risk
    # ATR ~ 200, sl_distance ~ 200, notional = 10 / (200/60000) = 3000
    # leverage = 3000/1000 = 3x
    assert 2000 <= result.requested_size_usd <= 4000
    assert 2.0 <= result.requested_leverage <= 4.0
    assert result.filled
    assert result.entry_oid == 100 and result.tp_oid == 101 and result.sl_oid == 102


def test_long_direction_short_handling() -> None:
    mgr = OrderManager(_FakeExchange(), _FakeInfo(), bankroll=1000.0)
    sig = CascadeSignal(
        symbol="BTC", timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
        direction=SignalDirection.LONG, confidence=0.7, reason="test", funding_rate=0.0,
    )
    candles = _candles()
    result = mgr.execute(sig, candles, current_price=60000.0)
    assert result.side == "long"
    # For a LONG, TP > entry and SL < entry
    assert result.tp_px > result.entry_px
    assert result.sl_px < result.entry_px


def test_tick_rounding_btc() -> None:
    mgr = OrderManager(_FakeExchange(), _FakeInfo(), bankroll=1000.0)
    candles = _candles()
    result = mgr.execute(_short_signal(), candles, current_price=63789.7)
    # BTC tick = $1, so entry should round to integer
    assert result.entry_px == int(result.entry_px)


def test_size_capped_at_max_leverage() -> None:
    # With a stop that's extremely far from entry, notional would exceed
    # max_leverage * bankroll; verify we cap.
    mgr = OrderManager(_FakeExchange(), _FakeInfo(), bankroll=1000.0,
                        risk_per_trade_pct=0.5,  # huge risk %
                        max_leverage=2.0)         # tight leverage cap
    candles = _candles()
    result = mgr.execute(_short_signal(), candles, current_price=60000.0)
    # 50% of 1000 = $500 risk; with ATR ~200, sl ~200, that's still
    # well under the 2x * $1000 = $2000 cap, so leverage should be capped
    assert result.requested_leverage <= 2.0 + 0.01


def test_bulk_orders_called_with_positionTpsl_grouping() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    mgr.execute(_short_signal(), _candles(), current_price=60000.0)
    assert len(fake.calls) == 1
    requests, grouping = fake.calls[0]
    assert grouping == "positionTpsl"
    assert len(requests) == 3
    assert requests[0]["order_type"] == {"limit": {"tif": "Gtc"}}
    assert requests[1]["order_type"]["trigger"]["tpsl"] == "tp"
    assert requests[2]["order_type"]["trigger"]["tpsl"] == "sl"
    # All three are reduce_only=True except entry
    assert requests[0]["reduce_only"] is False
    assert requests[1]["reduce_only"] is True
    assert requests[2]["reduce_only"] is True


def test_exchange_error_propagates() -> None:
    fake = _FakeExchange(raise_exc=RuntimeError("network blip"))
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    result = mgr.execute(_short_signal(), _candles(), current_price=60000.0)
    assert not result.filled
    assert "network blip" in (result.error or "")


def test_inner_order_error_parsed() -> None:
    fake = _FakeExchange(response={
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"error": "Price must be divisible by tick size. asset=3"},
                ]
            },
        },
    })
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    result = mgr.execute(_short_signal(), _candles(), current_price=60000.0)
    assert not result.filled
    assert "tick size" in (result.error or "")

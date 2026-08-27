"""Tests for the order manager sizing/validation logic.

These tests DO NOT call the exchange (no real orders placed).
They cover: ATR calc, size calc, tick rounding, risk rejection paths,
and the v1 trading allowlist.
"""
from pathlib import Path

import pandas as pd
import pytest

from src.execution.order_manager import BracketOrderIntent, OrderManager, OrderResult, V1_TRADE_SYMBOLS
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

    def bulk_cancel(self, requests):
        self.calls.append(("cancel", requests))
        return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}


class _FakeInfo:
    def meta(self):
        return {
            "universe": [
                {"name": "BTC", "szDecimals": 5},
                {"name": "ETH", "szDecimals": 4},
                {"name": "HYPE", "szDecimals": 2},
            ]
        }


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


def _intent(symbol: str = "BTC", side: str = "long") -> BracketOrderIntent:
    if symbol == "BTC":
        entry = 60000.0
        sl = 59800.0 if side == "long" else 60200.0
        tp = 60400.0 if side == "long" else 59600.0
    elif symbol == "HYPE":
        entry = 80.1234
        sl = entry * (0.99 if side == "long" else 1.01)
        tp = entry * (1.01 if side == "long" else 0.99)
    else:
        entry = 3000.0
        sl = entry * (0.99 if side == "long" else 1.01)
        tp = entry * (1.01 if side == "long" else 0.99)
    return BracketOrderIntent(
        signal_ts=pd.Timestamp("2026-08-01T12:00:00Z"),
        symbol=symbol,
        side=side,
        entry_px=entry,
        sl_px=sl,
        tp_px=tp,
        notional_usd=3000.0,
        reason="test paper-to-live bracket",
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
    # With an intentionally huge risk budget, notional would exceed
    # max_leverage * bankroll; verify we cap before risk approval.
    mgr = OrderManager(_FakeExchange(), _FakeInfo(), bankroll=1000.0,
                        risk_per_trade_pct=0.5,  # huge risk %
                        max_leverage=2.0)         # tight leverage cap
    candles = _candles()
    result = mgr.execute(_short_signal(), candles, current_price=60000.0)
    # 50% of 1000 = $500 risk; with ATR ~200, uncapped notional is far
    # above the 2x * $1000 cap, so leverage should be capped.
    assert result.requested_leverage <= 2.0 + 0.01


def test_bulk_orders_called_with_normalTpsl_grouping() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    mgr.execute(_short_signal(), _candles(), current_price=60000.0)
    assert len(fake.calls) == 1
    requests, grouping = fake.calls[0]
    assert grouping == "normalTpsl"
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


def test_no_trade_signal_rejected_not_short() -> None:
    mgr = OrderManager(_FakeExchange(), _FakeInfo(), bankroll=1000.0)
    sig = CascadeSignal(
        symbol="BTC", timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
        direction=SignalDirection.NO_TRADE, confidence=0.0,
        reason="test", funding_rate=0.0,
    )
    result = mgr.execute(sig, _candles(), current_price=60000.0)
    assert not result.filled
    assert result.side == "no_trade"
    assert "NO_TRADE" in (result.error or "")


def test_refuses_to_trade_without_atr_history() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    result = mgr.execute(_short_signal(), _candles(n=5), current_price=60000.0)
    assert not result.filled
    assert result.status == "rejected"
    assert "ATR" in (result.error or "")
    assert fake.calls == []


def test_orphan_entry_attempts_cancel_when_child_order_fails() -> None:
    fake = _FakeExchange(response={
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"resting": {"oid": 100}},
                    {"error": "bad trigger price"},
                    {"resting": {"oid": 102}},
                ]
            },
        },
    })
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    result = mgr.execute(_short_signal(), _candles(), current_price=60000.0)
    assert not result.filled
    assert result.status == "orphan_error"
    assert result.needs_reconciliation
    assert result.cancel_attempted
    assert fake.calls[-1] == ("cancel", [{"coin": "BTC", "oid": 100}])


def test_v1_allowlist_is_exactly_the_promoted_set() -> None:
    """Pinned deliberately. The allowlist is the last gate before real orders,
    so it must never grow by accident -- changing it should require changing
    this test too. ZEC added 2026-08-27 for the swing lane (n=109, PF 1.70,
    validated in both halves)."""
    assert V1_TRADE_SYMBOLS == frozenset({"BTC", "ETH", "HYPE", "ZEC"})


def test_refuses_to_trade_sol_research_symbol() -> None:
    """SOL is research-only. OrderManager must refuse to place an order
    for it, even with a valid LONG signal and full candle history."""
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    sig = CascadeSignal(
        symbol="SOL", timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
        direction=SignalDirection.LONG, confidence=0.85,
        reason="cascade detected", funding_rate=0.0001,
    )
    result = mgr.execute(sig, _candles(), current_price=73.0)
    assert not result.filled
    assert result.status == "rejected_v1_allowlist"
    assert "SOL" in (result.error or "")
    assert "research-only" in (result.error or "")
    assert fake.calls == []  # exchange never called


def test_refuses_to_trade_doge_research_symbol() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    sig = CascadeSignal(
        symbol="DOGE", timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
        direction=SignalDirection.SHORT, confidence=0.85,
        reason="cascade detected", funding_rate=0.0001,
    )
    result = mgr.execute(sig, _candles(), current_price=0.07)
    assert not result.filled
    assert result.status == "rejected_v1_allowlist"
    assert fake.calls == []


def test_btc_eth_and_hype_pass_allowlist() -> None:
    """Sanity: the v1 symbols do NOT trigger the allowlist guard."""
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    # We only need to confirm the allowlist doesn't reject. A real ATR
    # rejection is fine; just confirm the status is NOT
    # 'rejected_v1_allowlist'.
    for sym in ("BTC", "ETH", "HYPE"):
        sig = CascadeSignal(
            symbol=sym, timestamp=pd.Timestamp("2026-08-01T12:00:00Z"),
            direction=SignalDirection.SHORT, confidence=0.85,
            reason="test", funding_rate=0.0,
        )
        result = mgr.execute(sig, _candles(n=5), current_price=60000.0 if sym == "BTC" else 3000.0)
        assert result.status != "rejected_v1_allowlist", f"{sym} should pass allowlist"


def test_execute_bracket_intent_submits_exact_bracket() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)

    result = mgr.execute_bracket_intent(_intent())

    assert result.filled
    assert result.status == "submitted"
    assert result.entry_px == 60000.0
    assert result.sl_px == 59800.0
    assert result.tp_px == 60400.0
    assert len(fake.calls) == 1
    requests, grouping = fake.calls[0]
    assert grouping == "normalTpsl"
    assert len(requests) == 3
    assert requests[0]["reduce_only"] is False
    assert requests[1]["reduce_only"] is True
    assert requests[2]["reduce_only"] is True
    assert requests[1]["order_type"]["trigger"]["tpsl"] == "tp"
    assert requests[2]["order_type"]["trigger"]["tpsl"] == "sl"


def test_execute_bracket_intent_can_submit_stop_only_trailing_entry() -> None:
    fake = _FakeExchange(response={
        "status": "ok",
        "response": {
            "type": "order",
            "data": {
                "statuses": [
                    {"resting": {"oid": 100}},
                    {"resting": {"oid": 102}},
                ]
            },
        },
    })
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    intent = _intent()
    intent.tp_px = None

    result = mgr.execute_bracket_intent(intent)

    assert result.filled
    assert result.tp_oid is None
    assert result.sl_oid == 102
    requests, _ = fake.calls[0]
    assert len(requests) == 2
    assert requests[1]["order_type"]["trigger"]["tpsl"] == "sl"


def test_execute_bracket_intent_refuses_research_symbol() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)

    result = mgr.execute_bracket_intent(_intent(symbol="SOL"))

    assert not result.filled
    assert result.status == "rejected_v1_allowlist"
    assert fake.calls == []


def test_execute_bracket_intent_rounds_hype_to_three_decimal_tick() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)

    result = mgr.execute_bracket_intent(_intent(symbol="HYPE"))

    assert result.status != "rejected_v1_allowlist"
    assert result.entry_px == 80.123
    assert result.sl_px == 79.322
    assert result.tp_px == 80.925


def test_execute_bracket_intent_rejects_bad_stop_geometry() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    intent = _intent(side="long")
    intent.sl_px = 60100.0

    result = mgr.execute_bracket_intent(intent)

    assert not result.filled
    assert result.status == "rejected"
    assert "long stop" in (result.error or "")
    assert fake.calls == []


def test_execute_bracket_intent_risk_rejects_oversized_intent() -> None:
    fake = _FakeExchange()
    mgr = OrderManager(fake, _FakeInfo(), bankroll=1000.0)
    intent = _intent()
    intent.notional_usd = 9_000.0
    intent.sl_px = 59000.0

    result = mgr.execute_bracket_intent(intent)

    assert not result.filled
    assert result.status == "risk_rejected"
    assert result.risk_verdict == RiskVerdict.REJECTED_RISK_PCT
    assert fake.calls == []

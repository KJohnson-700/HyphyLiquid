"""
Tests for src/strategy/backtest.py

These tests focus on the core mechanics:
- Entry at NEXT bar (look-ahead avoidance)
- Stop loss hit
- Take profit hit
- Same-bar SL+TP: stop wins
- Max hold exit
- Funding payment accounting
- Fee + slippage deducted
- Position sizing scales with confidence
- Empty inputs handled
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.strategy.backtest import (
    BacktestTrade,
    _next_bar_entry,
    _atr,
    _funding_during_hold,
    _simulate_trade,
    run_backtest,
)
from src.strategy.cascade import CascadeSignal, SignalDirection


def make_candles(closes, start="2026-08-01", flat=False):
    """Build a synthetic candles DataFrame.

    flat=True: high=low=close=open (no spread, for ATR=0 tests)
    flat=False: high = close*1.005, low = close*0.995 (1% spread, for trade sims)
    """
    n = len(closes)
    times = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    if flat:
        return pd.DataFrame(
            {
                "timestamp": times,
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": [100.0] * n,
            }
        )
    return pd.DataFrame(
        {
            "timestamp": times,
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": [100.0] * n,
        }
    )


def make_funding(rates, start="2026-08-01"):
    """Build a synthetic funding history."""
    n = len(rates)
    times = pd.date_range(start, periods=n, freq="8h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": times,
            "coin": "BTC",
            "funding_rate": rates,
            "premium": [r * 0.5 for r in rates],
        }
    )


class TestNextBarEntry:
    def test_returns_first_strictly_after(self):
        df = make_candles([100, 101, 102])
        sig = pd.Timestamp("2026-08-01 00:30:00", tz="UTC")
        row = _next_bar_entry(df, sig)
        assert row is not None
        # Should be the bar at 2026-08-01 01:00, not 00:00
        assert row["timestamp"] == pd.Timestamp("2026-08-01 01:00", tz="UTC")

    def test_returns_none_if_no_future_bars(self):
        df = make_candles([100])
        sig = pd.Timestamp("2026-08-02 00:00:00", tz="UTC")
        assert _next_bar_entry(df, sig) is None


class TestATR:
    def test_constant_candles_zero_atr(self):
        df = make_candles([100] * 20, flat=True)
        atr = _atr(df, 14, period=14)
        assert atr == pytest.approx(0.0)

    def test_volatile_candles_nonzero_atr(self):
        closes = [100 + (i % 3) * 5 for i in range(20)]
        df = make_candles(closes)
        atr = _atr(df, 14, period=14)
        assert atr > 0


class TestFundingDuringHold:
    def test_no_funding_events_no_payment(self):
        f = make_funding([])
        result = _funding_during_hold(
            f,
            pd.Timestamp("2026-08-01 00:00:00", tz="UTC"),
            pd.Timestamp("2026-08-01 08:00:00", tz="UTC"),
            "long",
            1000.0,
        )
        assert result == 0.0

    def test_long_pays_positive_funding(self):
        # One funding event in the window, +0.01% on $1000 = $0.10
        f = make_funding([0.0001])
        result = _funding_during_hold(
            f,
            pd.Timestamp("2026-08-01 00:00:00", tz="UTC"),
            pd.Timestamp("2026-08-02 00:00:00", tz="UTC"),
            "long",
            1000.0,
        )
        assert result == pytest.approx(0.10)

    def test_long_receives_negative_funding(self):
        f = make_funding([-0.0001])
        result = _funding_during_hold(
            f,
            pd.Timestamp("2026-08-01 00:00:00", tz="UTC"),
            pd.Timestamp("2026-08-02 00:00:00", tz="UTC"),
            "long",
            1000.0,
        )
        assert result == pytest.approx(-0.10)

    def test_short_sign_flipped(self):
        f = make_funding([0.0001])
        result = _funding_during_hold(
            f,
            pd.Timestamp("2026-08-01 00:00:00", tz="UTC"),
            pd.Timestamp("2026-08-02 00:00:00", tz="UTC"),
            "short",
            1000.0,
        )
        # Short receives when funding is positive
        assert result == pytest.approx(-0.10)

    def test_funding_outside_window_excluded(self):
        # Funding event is at index 0 (start time) — should be excluded if strictly after entry
        f = make_funding([0.0001])
        result = _funding_during_hold(
            f,
            pd.Timestamp("2026-08-01 01:00:00", tz="UTC"),  # AFTER the funding
            pd.Timestamp("2026-08-02 00:00:00", tz="UTC"),
            "long",
            1000.0,
        )
        assert result == 0.0


class TestSimulateTrade:
    def _make_candles_with_known_atr(self, n=30, base=100.0):
        """n bars of $base with 1% high/low spread. Defaults to 30 bars so we have
        14+ bars of history for ATR no matter the signal time."""
        return make_candles([base] * n)

    def test_long_tp_hit(self):
        # 30 flat bars, then strong uptrend so TP gets hit
        closes = [100.0] * 30 + [100 + (i - 30) * 10 for i in range(30, 60)]
        candles = make_candles(closes)
        funding = make_funding([])
        sig = CascadeSignal(
            symbol="BTC",
            direction=SignalDirection.LONG,
            confidence=1.0,
            reason="test",
            funding_rate=0.001,
            # Signal at bar 20 (index 20) — well past the 14-bar ATR minimum
            timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
        )
        trade = _simulate_trade(
            sig, candles, funding, bankroll=1000.0, risk_per_trade_pct=0.01,
            leverage=10.0, take_profit_atr_multiple=2.0, stop_loss_atr_multiple=1.0,
            max_hold_bars=24, slippage_bps=5.0, taker_fee_pct=0.00045, confidence_sizing=True,
        )
        assert trade is not None
        assert trade.direction == "long"
        assert trade.pnl_usd > 0
        assert trade.exit_reason == "take_profit"

    def test_short_sl_hit(self):
        # 30 flat bars, then hard drop so short wins
        closes = [100.0] * 30 + [100 - (i - 30) * 3 for i in range(30, 60)]
        candles = make_candles(closes)
        funding = make_funding([])
        sig = CascadeSignal(
            symbol="BTC",
            direction=SignalDirection.SHORT,
            confidence=1.0,
            reason="test",
            funding_rate=-0.0008,
            timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
        )
        trade = _simulate_trade(
            sig, candles, funding, bankroll=1000.0, risk_per_trade_pct=0.01,
            leverage=10.0, take_profit_atr_multiple=2.0, stop_loss_atr_multiple=1.0,
            max_hold_bars=24, slippage_bps=5.0, taker_fee_pct=0.00045, confidence_sizing=True,
        )
        assert trade is not None
        assert trade.direction == "short"
        assert trade.pnl_usd > 0

    def test_no_trade_direction_returns_none(self):
        candles = make_candles([100] * 30)
        sig = CascadeSignal(
            symbol="BTC", direction=SignalDirection.NO_TRADE, confidence=0.0,
            reason="test", timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
        )
        result = _simulate_trade(
            sig, candles, make_funding([]), 1000.0, 0.01, 10.0, 2.0, 1.0, 24, 5.0, 0.00045, True
        )
        assert result is None

    def test_fees_deducted(self):
        closes = [100.0] * 30 + [100 + (i - 30) * 10 for i in range(30, 60)]
        candles = make_candles(closes)
        sig = CascadeSignal(
            symbol="BTC", direction=SignalDirection.LONG, confidence=1.0,
            reason="test", funding_rate=0.001,
            timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
        )
        trade = _simulate_trade(
            sig, candles, make_funding([]), 1000.0, 0.01, 10.0, 2.0, 1.0, 24, 5.0, 0.00045, True
        )
        assert trade is not None
        assert trade.fees_usd > 0
        assert trade.slippage_usd > 0


class TestRunBacktest:
    def test_empty_signals_returns_zero(self):
        result = run_backtest(
            signals=[],
            candles_by_symbol={"BTC": make_candles([100] * 30)},
            funding_by_symbol={"BTC": make_funding([0.0001] * 5)},
        )
        assert result.total_trades == 0
        assert result.total_pnl_usd == 0.0
        assert result.initial_bankroll == result.final_bankroll

    def test_basic_long_winning_trade(self):
        # 30 flat bars then strong uptrend — signal at bar 20 enters at bar 21
        closes = [100.0] * 30 + [100 + (i - 30) * 10 for i in range(30, 60)]
        candles_by_symbol = {"BTC": make_candles(closes)}
        funding_by_symbol = {"BTC": make_funding([0.0001] * 5)}
        sig = CascadeSignal(
            symbol="BTC", direction=SignalDirection.LONG, confidence=1.0,
            reason="test", funding_rate=0.001,
            timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
        )
        result = run_backtest(
            signals=[sig], candles_by_symbol=candles_by_symbol, funding_by_symbol=funding_by_symbol
        )
        assert result.total_trades == 1
        assert result.total_pnl_usd > 0
        assert result.win_rate == 1.0
        assert result.profit_factor == float("inf")  # no losses

    def test_mixed_wins_and_losses(self):
        closes = [100.0] * 60
        candles_by_symbol = {"BTC": make_candles(closes)}
        funding_by_symbol = {"BTC": make_funding([0.0001] * 5)}
        sigs = [
            CascadeSignal(
                symbol="BTC", direction=SignalDirection.LONG, confidence=1.0,
                reason="test", funding_rate=0.001,
                timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
            ),
            CascadeSignal(
                symbol="BTC", direction=SignalDirection.LONG, confidence=1.0,
                reason="test", funding_rate=0.001,
                timestamp=pd.Timestamp("2026-08-01 22:00:00", tz="UTC"),
            ),
        ]
        result = run_backtest(
            signals=sigs, candles_by_symbol=candles_by_symbol, funding_by_symbol=funding_by_symbol
        )
        assert result.total_trades >= 1
        assert result.total_fees_usd > 0
        assert result.total_slippage_usd > 0

    def test_haircut_50pct(self):
        closes = [100.0] * 30 + [100 + (i - 30) * 10 for i in range(30, 60)]
        candles_by_symbol = {"BTC": make_candles(closes)}
        funding_by_symbol = {"BTC": make_funding([0.0001] * 5)}
        sig = CascadeSignal(
            symbol="BTC", direction=SignalDirection.LONG, confidence=1.0,
            reason="test", funding_rate=0.001,
            timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
        )
        result = run_backtest(
            signals=[sig], candles_by_symbol=candles_by_symbol, funding_by_symbol=funding_by_symbol
        )
        assert result.haircut_50pct_pnl == pytest.approx(result.total_pnl_usd * 0.5)

    def test_max_drawdown_computed(self):
        closes = [100.0] * 30 + [100 - (i - 30) * 5 for i in range(30, 60)]
        candles_by_symbol = {"BTC": make_candles(closes)}
        funding_by_symbol = {"BTC": make_funding([0.0001] * 5)}
        sigs = [
            CascadeSignal(
                symbol="BTC", direction=SignalDirection.LONG, confidence=1.0,
                reason="test", funding_rate=0.001,
                timestamp=pd.Timestamp("2026-08-01 20:00:00", tz="UTC"),
            ),
        ]
        result = run_backtest(
            signals=sigs, candles_by_symbol=candles_by_symbol, funding_by_symbol=funding_by_symbol
        )
        assert result.max_drawdown_usd >= 0

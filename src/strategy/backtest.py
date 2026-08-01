"""
HyphyLiquid — Backtester for cascade counter-trade strategy

Simulates what would have happened if we'd acted on each CascadeSignal
against historical candles + funding data.

Honest defaults (Hyperliquid-realistic):
- Slippage: 5 bps per entry/exit (configurable, can be cranked up)
- Taker fees: 0.045% (HL default), 0.015% maker
- Funding: tracked from history; longs pay positive funding, shorts pay negative
- Look-ahead avoidance: enter at NEXT bar's open after signal, never current close
- Position sizing: risk_per_trade_pct of bankroll, with stop at ATR multiple
- Leverage: hard-capped, defaults to 10x (matches risk.py)
- Same-bar SL+TP: stop wins (conservative)
- Output includes a 50% degradation haircut ("real" expected performance)

A backtest is an UPPER BOUND on what live trading will deliver. The
haircut scenario is the more honest number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.strategy.cascade import CascadeSignal, SignalDirection

logger = logging.getLogger(__name__)


# ---------- Result types ----------


@dataclass
class BacktestTrade:
    symbol: str
    direction: str  # "long" or "short"
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    notional_usd: float = 0.0
    pnl_usd: float = 0.0
    funding_paid_usd: float = 0.0
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    confidence: float = 0.0

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0


@dataclass
class BacktestResult:
    trades: List[BacktestTrade]
    initial_bankroll: float
    final_bankroll: float
    total_pnl_usd: float
    total_fees_usd: float
    total_slippage_usd: float
    total_funding_paid_usd: float
    total_trades: int
    total_wins: int
    total_losses: int
    win_rate: float
    profit_factor: float
    avg_win_usd: float
    avg_loss_usd: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    sharpe_ratio: float
    return_pct: float
    avg_bars_held: float
    haircut_50pct_pnl: float  # 50% degradation scenario
    params: Dict = field(default_factory=dict)


# ---------- Per-trade simulation ----------


def _next_bar_entry(
    candles: pd.DataFrame, signal_time: pd.Timestamp
) -> Optional[pd.Series]:
    """
    Return the first candle whose timestamp is STRICTLY after signal_time.
    This avoids look-ahead bias — we never use the signal bar's open/close.
    """
    after = candles[candles["timestamp"] > signal_time]
    if after.empty:
        return None
    return after.iloc[0]


def _atr(candles: pd.DataFrame, end_idx: int, period: int = 14) -> float:
    """
    Average True Range over the last `period` bars, ending at end_idx.
    Uses standard True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    """
    if end_idx < period:
        period = end_idx
    if period <= 0:
        return 0.0
    window = candles.iloc[end_idx - period:end_idx]
    trs = []
    prev_close = None
    for _, row in window.iterrows():
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        if prev_close is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    return float(np.mean(trs)) if trs else 0.0


def _funding_during_hold(
    funding: pd.DataFrame,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    direction: str,
    notional_usd: float,
) -> float:
    """
    Sum funding payments for the position between entry and exit.
    Long pays positive funding; short pays negative funding.
    Returns total funding paid (positive = paid, negative = received).
    """
    if funding.empty:
        return 0.0
    mask = (funding["timestamp"] >= entry_time) & (funding["timestamp"] < exit_time)
    window = funding[mask]
    if window.empty:
        return 0.0
    rates = window["funding_rate"].astype(float)
    # Long pays +funding, short pays -funding (i.e. short receives +funding)
    sign = 1.0 if direction == "long" else -1.0
    return float((sign * rates * notional_usd).sum())


def _simulate_trade(
    signal: CascadeSignal,
    candles: pd.DataFrame,
    funding: pd.DataFrame,
    bankroll: float,
    risk_per_trade_pct: float,
    leverage: float,
    take_profit_atr_multiple: float,
    stop_loss_atr_multiple: float,
    max_hold_bars: int,
    slippage_bps: float,
    taker_fee_pct: float,
    confidence_sizing: bool,
    min_confidence_floor: float = 0.1,
) -> Optional[BacktestTrade]:
    """Simulate one signal. Returns None if entry conditions can't be met."""
    if signal.direction == SignalDirection.NO_TRADE:
        return None

    direction_str = signal.direction.value  # "long" or "short"
    candles_sorted = candles.sort_values("timestamp").reset_index(drop=True)

    # Find the candle INDEX where the signal fires
    # Match by exact timestamp first, then find the index
    entry_idx = candles_sorted[
        candles_sorted["timestamp"] == signal.timestamp
    ].index
    if len(entry_idx) == 0:
        # signal timestamp not in candles — find first candle after signal
        after_idx = candles_sorted[
            candles_sorted["timestamp"] > signal.timestamp
        ].index
        if len(after_idx) == 0:
            return None
        # Use the candle BEFORE this as the "signal" position for ATR purposes
        # and the candle AT this index as the "next bar" entry
        signal_idx = after_idx[0] - 1
        if signal_idx < 14:  # need 14 bars for ATR
            return None
        entry_candle = candles_sorted.iloc[after_idx[0]]
    else:
        signal_idx = entry_idx[0]
        # Need at least 1 bar after signal for entry
        if signal_idx + 1 >= len(candles_sorted):
            return None
        # Need 14 bars BEFORE for ATR
        if signal_idx < 14:
            return None
        entry_candle = candles_sorted.iloc[signal_idx + 1]

    # ATR at signal time (use last 14 bars ending at signal_idx)
    atr = _atr(candles_sorted, signal_idx + 1, period=14)
    if atr <= 0:
        return None

    # Stop and take-profit distances
    stop_distance = stop_loss_atr_multiple * atr
    tp_distance = take_profit_atr_multiple * atr

    # Position sizing: 1% of bankroll at risk, divided by stop distance
    confidence_mult = signal.confidence if confidence_sizing else 1.0
    confidence_mult = max(confidence_mult, min_confidence_floor)
    risk_usd = bankroll * risk_per_trade_pct * confidence_mult
    if stop_distance <= 0:
        return None
    position_size_base = risk_usd / stop_distance  # in units of the asset
    # Apply leverage cap
    raw_entry_price = float(entry_candle["open"])
    notional_usd = position_size_base * raw_entry_price
    if notional_usd > bankroll * leverage:
        notional_usd = bankroll * leverage
        position_size_base = notional_usd / raw_entry_price

    # Apply slippage on entry (adverse direction)
    slip_pct = slippage_bps / 10_000
    if direction_str == "long":
        entry_price = raw_entry_price * (1 + slip_pct)
    else:
        entry_price = raw_entry_price * (1 - slip_pct)

    # Stop and TP levels
    if direction_str == "long":
        stop_price = entry_price - stop_distance
        tp_price = entry_price + tp_distance
    else:
        stop_price = entry_price + stop_distance
        tp_price = entry_price - tp_distance

    # Walk forward bar by bar until exit condition
    bars_walked = 0
    exit_price = None
    exit_time = None
    exit_reason = ""
    # Start walking from the bar AFTER entry
    walk_start = entry_candle.name + 1  # index of next bar after entry
    for i in range(walk_start, min(walk_start + max_hold_bars, len(candles_sorted))):
        bar = candles_sorted.iloc[i]
        high = float(bar["high"])
        low = float(bar["low"])
        bars_walked += 1

        if direction_str == "long":
            hit_tp = high >= tp_price
            hit_sl = low <= stop_price
        else:
            hit_tp = low <= tp_price
            hit_sl = high >= stop_price

        if hit_tp and hit_sl:
            # Same bar: stop wins (conservative)
            exit_price = stop_price
            exit_reason = "stop_loss_same_bar_as_tp"
            exit_time = bar["timestamp"]
            break
        if hit_sl:
            exit_price = stop_price
            exit_reason = "stop_loss"
            exit_time = bar["timestamp"]
            break
        if hit_tp:
            exit_price = tp_price
            exit_reason = "take_profit"
            exit_time = bar["timestamp"]
            break
    else:
        # Max hold reached without TP/SL hit — exit at last close
        if walk_start < len(candles_sorted):
            last_bar = candles_sorted.iloc[min(walk_start + max_hold_bars - 1, len(candles_sorted) - 1)]
            exit_price = float(last_bar["close"])
            exit_time = last_bar["timestamp"]
            exit_reason = "max_hold"
        else:
            return None

    # Apply exit slippage
    if direction_str == "long":
        exit_price = exit_price * (1 - slip_pct)
    else:
        exit_price = exit_price * (1 + slip_pct)

    # Compute P&L
    if direction_str == "long":
        price_pnl = (exit_price - entry_price) * position_size_base
    else:
        price_pnl = (entry_price - exit_price) * position_size_base

    fees = (notional_usd * taker_fee_pct) + (notional_usd * taker_fee_pct)  # entry + exit
    slippage_cost = slip_pct * notional_usd * 2  # entry + exit adverse move
    funding_paid = _funding_during_hold(
        funding, entry_candle["timestamp"], exit_time, direction_str, notional_usd
    )

    pnl_usd = price_pnl - fees - funding_paid

    return BacktestTrade(
        symbol=signal.symbol,
        direction=direction_str,
        entry_time=entry_candle["timestamp"],
        entry_price=entry_price,
        exit_time=exit_time,
        exit_price=exit_price,
        notional_usd=notional_usd,
        pnl_usd=pnl_usd,
        funding_paid_usd=funding_paid,
        fees_usd=fees,
        slippage_usd=slippage_cost,
        exit_reason=exit_reason,
        bars_held=bars_walked,
        confidence=signal.confidence,
    )


# ---------- Full backtest ----------


def run_backtest(
    signals: List[CascadeSignal],
    candles_by_symbol: Dict[str, pd.DataFrame],
    funding_by_symbol: Dict[str, pd.DataFrame],
    initial_bankroll: float = 1000.0,
    risk_per_trade_pct: float = 0.01,
    leverage: float = 10.0,
    take_profit_atr_multiple: float = 2.0,
    stop_loss_atr_multiple: float = 1.0,
    max_hold_bars: int = 24,
    slippage_bps: float = 5.0,
    taker_fee_pct: float = 0.00045,
    confidence_sizing: bool = True,
    max_concurrent_positions: int = 1,
    haircut_pct: float = 0.5,
) -> BacktestResult:
    """
    Run the backtest over a list of signals, with per-symbol candle + funding.

    Args:
        signals: list of CascadeSignal objects to evaluate
        candles_by_symbol: {symbol: candles_df} with columns timestamp, open, high, low, close, volume
        funding_by_symbol: {symbol: funding_df} with columns timestamp, coin, funding_rate, premium
        initial_bankroll: starting capital
        risk_per_trade_pct: 0.01 = 1% per trade
        leverage: cap on notional vs bankroll
        take_profit_atr_multiple: TP at N * ATR
        stop_loss_atr_multiple: SL at N * ATR
        max_hold_bars: exit if neither TP nor SL hit after this many bars
        slippage_bps: per-side slippage in basis points
        taker_fee_pct: taker fee as fraction (0.00045 = 0.045%)
        confidence_sizing: scale position by signal confidence
        max_concurrent_positions: 1 = no overlap (simpler)
        haircut_pct: degradation factor for "real" expected performance

    Returns:
        BacktestResult with all metrics
    """
    # Sort signals by time so we can simulate them in order
    sorted_signals = sorted(
        [s for s in signals if s.direction != SignalDirection.NO_TRADE],
        key=lambda s: s.timestamp if s.timestamp is not None else pd.Timestamp.min,
    )

    bankroll = initial_bankroll
    open_positions: List[BacktestTrade] = []
    closed_trades: List[BacktestTrade] = []

    for sig in sorted_signals:
        candles = candles_by_symbol.get(sig.symbol)
        funding = funding_by_symbol.get(sig.symbol)
        if candles is None or candles.empty or funding is None:
            continue

        # Force-close any open position in the same symbol (simple mode)
        if max_concurrent_positions == 1:
            for pos in list(open_positions):
                if pos.symbol == sig.symbol:
                    # Mark them closed at the signal bar's open
                    if pos.exit_time is None:
                        pos.exit_time = sig.timestamp
                        if candles is not None and not candles.empty:
                            sig_bar = candles[
                                candles["timestamp"] >= sig.timestamp
                            ]
                            if not sig_bar.empty:
                                pos.exit_price = float(sig_bar.iloc[0]["open"])
                                pos.exit_reason = "new_signal_replace"
                                # Recompute P&L (very rough — skip the funding update for simplicity)
                                direction_sign = (
                                    1.0 if pos.direction == "long" else -1.0
                                )
                                pos.pnl_usd = (
                                    direction_sign
                                    * (pos.exit_price - pos.entry_price)
                                    * (pos.notional_usd / pos.entry_price)
                                    - pos.fees_usd
                                )
                        open_positions.remove(pos)
                        if pos.exit_time is not None and pos.exit_price is not None:
                            closed_trades.append(pos)

        if len(open_positions) >= max_concurrent_positions:
            continue

        trade = _simulate_trade(
            sig,
            candles,
            funding,
            bankroll,
            risk_per_trade_pct,
            leverage,
            take_profit_atr_multiple,
            stop_loss_atr_multiple,
            max_hold_bars,
            slippage_bps,
            taker_fee_pct,
            confidence_sizing,
        )
        if trade is None:
            continue
        closed_trades.append(trade)
        bankroll += trade.pnl_usd

    # Close any remaining open positions at last known price
    for pos in open_positions:
        if pos.symbol in candles_by_symbol and not candles_by_symbol[pos.symbol].empty:
            last_close = float(candles_by_symbol[pos.symbol].iloc[-1]["close"])
            direction_sign = 1.0 if pos.direction == "long" else -1.0
            pos.exit_price = last_close
            pos.exit_time = candles_by_symbol[pos.symbol].iloc[-1]["timestamp"]
            pos.exit_reason = "backtest_end"
            pos.pnl_usd = (
                direction_sign
                * (pos.exit_price - pos.entry_price)
                * (pos.notional_usd / pos.entry_price)
                - pos.fees_usd
            )
            bankroll += pos.pnl_usd
            closed_trades.append(pos)

    return _summarize(
        closed_trades,
        initial_bankroll=initial_bankroll,
        final_bankroll=bankroll,
        haircut_pct=haircut_pct,
        params={
            "risk_per_trade_pct": risk_per_trade_pct,
            "leverage": leverage,
            "tp_atr_mult": take_profit_atr_multiple,
            "sl_atr_mult": stop_loss_atr_multiple,
            "max_hold_bars": max_hold_bars,
            "slippage_bps": slippage_bps,
            "taker_fee_pct": taker_fee_pct,
            "confidence_sizing": confidence_sizing,
        },
    )


def _summarize(
    trades: List[BacktestTrade],
    initial_bankroll: float,
    final_bankroll: float,
    haircut_pct: float,
    params: Dict,
) -> BacktestResult:
    """Aggregate trades into a BacktestResult."""
    n = len(trades)
    if n == 0:
        return BacktestResult(
            trades=trades,
            initial_bankroll=initial_bankroll,
            final_bankroll=initial_bankroll,
            total_pnl_usd=0.0,
            total_fees_usd=0.0,
            total_slippage_usd=0.0,
            total_funding_paid_usd=0.0,
            total_trades=0,
            total_wins=0,
            total_losses=0,
            win_rate=0.0,
            profit_factor=0.0,
            avg_win_usd=0.0,
            avg_loss_usd=0.0,
            max_drawdown_usd=0.0,
            max_drawdown_pct=0.0,
            sharpe_ratio=0.0,
            return_pct=0.0,
            avg_bars_held=0.0,
            haircut_50pct_pnl=0.0,
            params=params,
        )

    pnls = [t.pnl_usd for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)
    total_fees = sum(t.fees_usd for t in trades)
    total_slip = sum(t.slippage_usd for t in trades)
    total_fund = sum(t.funding_paid_usd for t in trades)
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Equity curve for drawdown
    equity = [initial_bankroll]
    for p in pnls:
        equity.append(equity[-1] + p)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0.0

    # Sharpe on per-trade returns (annualized, ~252 trading days)
    pnl_arr = np.array(pnls)
    if len(pnl_arr) > 1 and np.std(pnl_arr) > 0:
        sharpe = (np.mean(pnl_arr) / np.std(pnl_arr)) * np.sqrt(252)
    else:
        sharpe = 0.0

    return_pct = (total_pnl / initial_bankroll) * 100
    avg_bars = sum(t.bars_held for t in trades) / n
    haircut_pnl = total_pnl * (1 - haircut_pct)

    return BacktestResult(
        trades=trades,
        initial_bankroll=initial_bankroll,
        final_bankroll=final_bankroll,
        total_pnl_usd=total_pnl,
        total_fees_usd=total_fees,
        total_slippage_usd=total_slip,
        total_funding_paid_usd=total_fund,
        total_trades=n,
        total_wins=len(wins),
        total_losses=len(losses),
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win_usd=avg_win,
        avg_loss_usd=avg_loss,
        max_drawdown_usd=max_dd,
        max_drawdown_pct=max_dd_pct,
        sharpe_ratio=sharpe,
        return_pct=return_pct,
        avg_bars_held=avg_bars,
        haircut_50pct_pnl=haircut_pnl,
        params=params,
    )

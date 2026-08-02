"""
HyphyLiquid - Liquidation backtester.

For each detected liquidation event, simulate entering a FADE trade
(against the cascade direction) on the next bar, hold for a fixed
horizon, and compute the P&L.

This is the harness to validate the cascade-fade hypothesis on REAL
liquidation data, instead of the failed funding-rate proxies.

Inputs:
  - liquidation_events: list of dicts with ts, symbol, side, price_avg
  - candles_by_symbol: dict of symbol -> DataFrame with timestamp+ohlcv
  - entry_window: how many bars after the event to enter (default 1)
  - exit_horizon_bars: how many bars to hold (default 24)
  - slippage_bps, fee_pct: transaction cost assumptions

Returns:
  - dict of per-trade P&L plus aggregate metrics
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class LiquidationTrade:
    """A simulated fade trade against a liquidation event."""
    symbol: str
    event_ts: pd.Timestamp
    event_side: str
    event_price: float
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    pnl_pct: float  # signed: positive = profit
    bars_held: int
    exit_reason: str  # "horizon", "tp", "sl"


@dataclass
class LiquidationBacktestResult:
    trades: list[LiquidationTrade]
    total: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl_pct: float
    median_pnl_pct: float
    profit_factor: float
    avg_bars_held: float
    by_horizon: dict  # {horizon_bars: {n, win_rate, avg_pnl}}


def _find_entry(
    candles: pd.DataFrame, event_ts: pd.Timestamp, entry_window: int
) -> Optional[pd.Timestamp]:
    """Find the entry bar: the (entry_window)-th bar AFTER event_ts."""
    future = candles[candles.index > event_ts]
    if len(future) < entry_window:
        return None
    return future.index[entry_window - 1]


def _find_exit_horizon(
    candles: pd.DataFrame, entry_ts: pd.Timestamp, horizon_bars: int
) -> Optional[pd.Timestamp]:
    future = candles[candles.index > entry_ts]
    if len(future) < horizon_bars:
        return None
    return future.index[horizon_bars - 1]


def run_liquidation_backtest(
    liquidation_events: list[dict],
    candles_by_symbol: dict[str, pd.DataFrame],
    entry_window: int = 1,
    exit_horizons: tuple[int, ...] = (1, 4, 24, 72),
    slippage_bps: float = 5.0,
    fee_pct: float = 0.00045,
) -> dict[int, LiquidationBacktestResult]:
    """
    For each liquidation event, simulate entering a fade trade.
    Returns a dict of horizon_bars -> result.
    """
    # Build per-symbol candle lookup
    candles_indexed: dict[str, pd.DataFrame] = {}
    for sym, df in candles_by_symbol.items():
        c = df.copy()
        c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
        candles_indexed[sym] = c.set_index("timestamp").sort_index()

    # For each horizon, simulate
    results: dict[int, list[LiquidationTrade]] = {h: [] for h in exit_horizons}
    skipped = 0
    for ev in liquidation_events:
        sym = ev["symbol"]
        if sym not in candles_indexed:
            skipped += 1
            continue
        candles = candles_indexed[sym]
        event_ts = pd.Timestamp(ev["ts"])
        entry_ts = _find_entry(candles, event_ts, entry_window)
        if entry_ts is None:
            skipped += 1
            continue
        # Apply slippage: enter at slightly worse price
        slip = slippage_bps / 10000
        if ev["side"] == "B":  # bid-side burst, fading = SHORT
            fade_side = "short"
            entry_price = candles.loc[entry_ts, "close"] * (1 + slip)
        else:  # ask-side burst, fading = LONG
            fade_side = "long"
            entry_price = candles.loc[entry_ts, "close"] * (1 - slip)

        for h in exit_horizons:
            exit_ts = _find_exit_horizon(candles, entry_ts, h)
            if exit_ts is None:
                continue
            exit_price = candles.loc[exit_ts, "close"]
            # Apply fees
            if fade_side == "long":
                pnl_pct = (exit_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - exit_price) / entry_price
            pnl_pct -= 2 * fee_pct  # entry + exit fee
            bars_held = h
            results[h].append(
                LiquidationTrade(
                    symbol=sym,
                    event_ts=event_ts,
                    event_side=ev["side"],
                    event_price=ev.get("price_avg", 0),
                    entry_ts=entry_ts,
                    entry_price=entry_price,
                    exit_ts=exit_ts,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    bars_held=bars_held,
                    exit_reason="horizon",
                )
            )

    out: dict[int, LiquidationBacktestResult] = {}
    for h, trades in results.items():
        if not trades:
            out[h] = LiquidationBacktestResult(
                trades=[], total=0, wins=0, losses=0,
                win_rate=0.0, avg_pnl_pct=0.0, median_pnl_pct=0.0,
                profit_factor=0.0, avg_bars_held=0.0, by_horizon={},
            )
            continue
        wins = sum(1 for t in trades if t.pnl_pct > 0)
        losses = sum(1 for t in trades if t.pnl_pct <= 0)
        pnls = [t.pnl_pct for t in trades]
        gross_wins = sum(p for p in pnls if p > 0)
        gross_losses = abs(sum(p for p in pnls if p < 0))
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        out[h] = LiquidationBacktestResult(
            trades=trades,
            total=len(trades),
            wins=wins,
            losses=losses,
            win_rate=wins / len(trades),
            avg_pnl_pct=sum(pnls) / len(pnls),
            median_pnl_pct=pd.Series(pnls).median(),
            profit_factor=pf,
            avg_bars_held=sum(t.bars_held for t in trades) / len(trades),
            by_horizon={},
        )
    return out

"""
strategy_search.py — code-grab strategy discovery loop.

Adapted from moondevonyt/Harvard-Algorithmic-Trading-with-AI (BB squeeze + ADX)
and chainstacklabs/hyperliquid-trading-bot (grid). Reimplemented without TA-Lib
so the project's notebooklm-cli venv can run it.

Usage:
  python scripts/strategy_search.py
  python scripts/strategy_search.py --strategy bb_squeeze --symbol BTC --tf 6h
  python scripts/strategy_search.py --symbol BTC --tf 1d --top 5
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMPORTS_DIR = PROJECT_ROOT / "research" / "imports"
RESULTS_DIR = PROJECT_ROOT / "data" / "strategy_search"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Indicators (pure numpy/pandas, no TA-Lib)
# -----------------------------------------------------------------------------

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    tr = true_range(high, low, close)
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    return mid + std * sd, mid, mid - std * sd


def keltner(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20, atr_mult: float = 1.5):
    mid = close.rolling(period).mean()
    tr = true_range(high, low, close)
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return mid + atr_mult * atr, mid, mid - atr_mult * atr


# -----------------------------------------------------------------------------
# Strategy signals (returns a DataFrame with `signal` and meta cols)
# -----------------------------------------------------------------------------

@dataclass
class StrategyResult:
    name: str
    symbol: str
    timeframe: str
    n_bars: int
    n_trades: int
    win_rate: float
    profit_factor: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    net_return_pct: float
    avg_return_pct: float
    median_return_pct: float
    avg_hold_bars: float
    exposure_pct: float
    total_funding_usd: float = 0.0
    total_pnl_usd: float = 0.0
    start: str = ""
    end: str = ""
    raw_equity_tail: list = field(default_factory=list)


def backtest_long_short(
    df: pd.DataFrame,
    signal: pd.Series,
    take_profit: float,
    stop_loss: float,
    max_hold: int | None,
    cost_bps: float = 8.0,
    initial_equity: float = 10_000.0,
    risk_per_trade: float = 0.01,
) -> dict:
    """Simple long/short backtest with bracket TP/SL. signal=1 long, -1 short, 0 flat."""
    cash = initial_equity
    position = 0  # 0/1/-1
    entry_price = 0.0
    entry_idx = 0
    n_bars = len(df)
    trades: list[dict] = []
    equity_curve: list[float] = [initial_equity]
    in_trade_bars = 0

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    sig = signal.values
    risk_dollars = initial_equity * risk_per_trade

    for i in range(1, n_bars):
        # Mark-to-market equity
        if position != 0:
            mtm = cash + (closes[i] - entry_price) * position * (risk_dollars / max(stop_loss * entry_price, 1e-9))
        else:
            mtm = cash
        equity_curve.append(mtm)

        # Check exit first
        if position != 0:
            in_trade_bars += 1
            stop_price = entry_price * (1 + stop_loss * (-position))  # long: stop below, short: stop above
            tp_price = entry_price * (1 + take_profit * position)
            exit_reason = None
            if position == 1:
                if lows[i] <= stop_price:
                    exit_reason = "stop"
                    exit_price = stop_price
                elif highs[i] >= tp_price:
                    exit_reason = "tp"
                    exit_price = tp_price
            else:  # short
                if highs[i] >= stop_price:
                    exit_reason = "stop"
                    exit_price = stop_price
                elif lows[i] <= tp_price:
                    exit_reason = "tp"
                    exit_price = tp_price
            if exit_reason is None and max_hold is not None and in_trade_bars >= max_hold:
                exit_reason = "timeout"
                exit_price = closes[i]
            if exit_reason is not None:
                gross_ret = (exit_price - entry_price) / entry_price * position
                net_ret = gross_ret - (cost_bps / 10_000.0)
                pnl = risk_dollars * gross_ret  # sizing is on stop-loss-bps risk
                cash += pnl - (cost_bps / 10_000.0) * risk_dollars
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "side": "long" if position == 1 else "short",
                    "entry": entry_price,
                    "exit": exit_price,
                    "gross_return_pct": gross_ret * 100,
                    "net_return_pct": net_ret * 100,
                    "hold_bars": i - entry_idx,
                    "exit_reason": exit_reason,
                })
                position = 0
                in_trade_bars = 0

        # New entry
        if position == 0 and sig[i] != 0 and i + 1 < n_bars:
            position = int(sig[i])
            entry_price = closes[i]
            entry_idx = i
            in_trade_bars = 0

    # Force close at end
    if position != 0:
        gross_ret = (closes[-1] - entry_price) / entry_price * position
        net_ret = gross_ret - (cost_bps / 10_000.0)
        cash += risk_dollars * gross_ret - (cost_bps / 10_000.0) * risk_dollars
        trades.append({
            "entry_idx": entry_idx,
            "exit_idx": n_bars - 1,
            "side": "long" if position == 1 else "short",
            "entry": entry_price,
            "exit": closes[-1],
            "gross_return_pct": gross_ret * 100,
            "net_return_pct": net_ret * 100,
            "hold_bars": n_bars - 1 - entry_idx,
            "exit_reason": "end_of_data",
        })

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "final_equity": cash,
        "n_bars": n_bars,
    }


def backtest_inter_asset_funding_spread(
    df: pd.DataFrame,
    threshold_bps_per_hr: float = 0.5,  # enter if spread >= 0.5 bps/hr
    max_hold: int = 24,
    stop_loss_pct: float = 0.03,
    take_profit_pct: float = 0.02,
    cost_bps_per_leg: float = 4.0,
    notional_per_leg: float = 5_000.0,
    initial_cash: float = 10_000.0,
) -> dict:
    """2-leg delta-neutral inter-asset funding spread.

    df: wide panel with fund_<SYM>, px_<SYM>, hi_<SYM>, lo_<SYM> columns
    Long the lowest-funding symbol, short the highest-funding symbol.
    Both legs equal notional. P&L = price moves + funding collected.
    """
    symbols = sorted([c.replace("fund_", "") for c in df.columns if c.startswith("fund_")])
    if not symbols:
        raise ValueError("no fund_<SYM> columns in df")

    cash = initial_cash
    in_trade = False
    long_sym = None
    short_sym = None
    long_entry = 0.0
    short_entry = 0.0
    long_funding = 0.0
    short_funding = 0.0
    entry_idx = 0

    trades: list[dict] = []
    equity_curve: list[float] = [initial_cash]

    for i in range(1, len(df)):
        row = df.iloc[i]

        # Mark to market
        if in_trade:
            long_pct = (row[f"px_{long_sym}"] - long_entry) / long_entry
            short_pct = (short_entry - row[f"px_{short_sym}"]) / short_entry
            long_pnl = long_pct * notional_per_leg
            short_pnl = short_pct * notional_per_leg
            # funding accrual: 1 bar = 1 hour, multiply funding rate * notional
            funding_pnl = (short_funding - long_funding) * notional_per_leg * 1.0
            mtm = cash + long_pnl + short_pnl + funding_pnl
        else:
            mtm = cash
        equity_curve.append(mtm)

        # Check exit
        exit_now = False
        exit_reason = None
        if in_trade:
            long_pct = (row[f"px_{long_sym}"] - long_entry) / long_entry
            short_pct = (short_entry - row[f"px_{short_sym}"]) / short_entry
            if long_pct <= -stop_loss_pct or short_pct <= -stop_loss_pct:
                exit_now = True
                exit_reason = "stop_loss"
            elif long_pct >= take_profit_pct and short_pct >= take_profit_pct:
                exit_now = True
                exit_reason = "take_profit"
            elif (i - entry_idx) >= max_hold:
                exit_now = True
                exit_reason = "timeout"
            if exit_now:
                long_pnl = long_pct * notional_per_leg
                short_pnl = short_pct * notional_per_leg
                # Funding P&L: longs PAY funding when rate > 0, shorts RECEIVE
                # Long P&L from funding = -long_funding * notional * hours
                # Short P&L from funding = +short_funding * notional * hours
                # Net = (short_funding - long_funding) * notional * hours = spread * notional * hours
                funding_pnl = (short_funding - long_funding) * notional_per_leg * (i - entry_idx)
                net_pnl = long_pnl + short_pnl + funding_pnl - 2 * (cost_bps_per_leg / 10_000.0) * notional_per_leg
                cash += net_pnl
                trades.append({
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "long_sym": long_sym,
                    "short_sym": short_sym,
                    "long_pnl_usd": long_pnl,
                    "short_pnl_usd": short_pnl,
                    "funding_pnl_usd": funding_pnl,
                    "net_pnl_usd": net_pnl,
                    "net_return_pct": (net_pnl / initial_cash) * 100,
                    "long_pct": long_pct * 100,
                    "short_pct": short_pct * 100,
                    "hold_bars": i - entry_idx,
                    "exit_reason": exit_reason,
                    "spread_bps_hr": (long_funding + short_funding) * 10_000,
                })
                in_trade = False

        # New entry
        if not in_trade:
            fund_map = {s: row[f"fund_{s}"] for s in symbols}
            sorted_f = sorted(fund_map.items(), key=lambda x: x[1])
            low_sym = sorted_f[0][0]
            high_sym = sorted_f[-1][0]
            spread = fund_map[high_sym] - fund_map[low_sym]
            if spread * 10_000 >= threshold_bps_per_hr and i + 1 < len(df):
                long_sym = low_sym
                short_sym = high_sym
                long_entry = row[f"px_{long_sym}"]
                short_entry = row[f"px_{short_sym}"]
                long_funding = fund_map[low_sym]
                short_funding = fund_map[high_sym]
                entry_idx = i
                in_trade = True

    # Force close at end
    if in_trade:
        long_pct = (df[f"px_{long_sym}"].iloc[-1] - long_entry) / long_entry
        short_pct = (short_entry - df[f"px_{short_sym}"].iloc[-1]) / short_entry
        long_pnl = long_pct * notional_per_leg
        short_pnl = short_pct * notional_per_leg
        funding_pnl = (short_funding - long_funding) * notional_per_leg * (len(df) - 1 - entry_idx)
        net_pnl = long_pnl + short_pnl + funding_pnl - 2 * (cost_bps_per_leg / 10_000.0) * notional_per_leg
        cash += net_pnl
        trades.append({
            "entry_idx": entry_idx,
            "exit_idx": len(df) - 1,
            "long_sym": long_sym,
            "short_sym": short_sym,
            "long_pnl_usd": long_pnl,
            "short_pnl_usd": short_pnl,
            "funding_pnl_usd": funding_pnl,
            "net_pnl_usd": net_pnl,
            "net_return_pct": (net_pnl / initial_cash) * 100,
            "long_pct": long_pct * 100,
            "short_pct": short_pct * 100,
            "hold_bars": len(df) - 1 - entry_idx,
            "exit_reason": "end_of_data",
            "spread_bps_hr": (long_funding + short_funding) * 10_000,
        })

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "final_equity": cash,
        "n_bars": len(df),
    }


def score_2leg(trades: list[dict], equity_curve: list[float], n_bars: int) -> dict:
    """Score for 2-leg strategies: focus on funding collected, total return, edge consistency."""
    pnls = np.array([t["net_return_pct"] for t in trades])
    funding_only = np.array([t.get("funding_pnl_usd", 0) / 10_000 * 100 for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = (len(wins) / len(pnls) * 100) if len(pnls) else 0
    sum_pos = wins.sum() if len(wins) else 0
    sum_neg = losses.sum() if len(losses) else 0
    pf = (sum_pos / abs(sum_neg)) if sum_neg != 0 else 999
    eq = np.array(equity_curve)
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    sharpe = (rets.mean() / rets.std() * np.sqrt(365 * 24)) if rets.std() > 0 else 0
    downside = rets[rets < 0]
    sortino = (rets.mean() / downside.std() * np.sqrt(365 * 24)) if len(downside) > 0 and downside.std() > 0 else 0
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = dd.min() * 100
    n_trades = len(trades)
    avg_hold = float(np.mean([t["hold_bars"] for t in trades])) if trades else 0
    total_funding = sum(t.get("funding_pnl_usd", 0) for t in trades)
    total_pnl = sum(t.get("net_pnl_usd", 0) for t in trades)
    return {
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": pf,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": max_dd,
        "net_return_pct": (eq[-1] / eq[0] - 1) * 100,
        "avg_return_pct": float(pnls.mean()) if len(pnls) else 0,
        "median_return_pct": float(np.median(pnls)) if len(pnls) else 0,
        "avg_hold_bars": avg_hold,
        "total_funding_usd": total_funding,
        "total_pnl_usd": total_pnl,
        "exposure_pct": (sum(t["hold_bars"] for t in trades) / n_bars * 100) if n_bars else 0,
    }


def score(trades: list[dict], equity_curve: list[float], n_bars: int) -> dict:
    pnls = np.array([t["net_return_pct"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = (len(wins) / len(pnls) * 100) if len(pnls) else 0
    sum_pos = wins.sum() if len(wins) else 0
    sum_neg = losses.sum() if len(losses) else 0
    pf = (sum_pos / abs(sum_neg)) if sum_neg != 0 else 999
    eq = np.array(equity_curve)
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    sharpe = (rets.mean() / rets.std() * np.sqrt(365 * 4)) if rets.std() > 0 else 0  # 6h bars, ~4/day
    downside = rets[rets < 0]
    sortino = (rets.mean() / downside.std() * np.sqrt(365 * 4)) if len(downside) > 0 and downside.std() > 0 else 0
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = dd.min() * 100
    n_trades = len(trades)
    avg_hold = float(np.mean([t["hold_bars"] for t in trades])) if trades else 0
    total_in_trade = sum(t["hold_bars"] for t in trades)
    exposure = (total_in_trade / n_bars * 100) if n_bars else 0
    return {
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": pf,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": max_dd,
        "net_return_pct": (eq[-1] / eq[0] - 1) * 100,
        "avg_return_pct": float(pnls.mean()) if len(pnls) else 0,
        "median_return_pct": float(np.median(pnls)) if len(pnls) else 0,
        "avg_hold_bars": avg_hold,
        "exposure_pct": exposure,
    }


# -----------------------------------------------------------------------------
# Strategy 1: BB Squeeze + ADX (adapted from moondev Harvard bot)
# -----------------------------------------------------------------------------

def signal_bb_squeeze_adx(
    df: pd.DataFrame,
    bb_window: int = 20,
    bb_std: float = 2.0,
    keltner_window: int = 20,
    keltner_atr_mult: float = 1.5,
    adx_period: int = 14,
    adx_threshold: float = 25.0,
) -> pd.Series:
    up, mid, lo = bollinger(df["close"], bb_window, bb_std)
    ku, km, kl = keltner(df["high"], df["low"], df["close"], keltner_window, keltner_atr_mult)
    squeeze = (up < ku) & (lo > kl)  # BB inside KC = squeeze on
    adx_v = adx(df["high"], df["low"], df["close"], adx_period)
    # Signal: 1 long if squeeze just released (was on, now off) + close > upper BB + ADX > threshold
    #        -1 short if squeeze just released + close < lower BB + ADX > threshold
    squeeze_was_on = squeeze.shift(1).fillna(False)
    squeeze_now = squeeze.fillna(False)
    released = squeeze_was_on & ~squeeze_now
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[(released) & (df["close"] > up) & (adx_v > adx_threshold)] = 1
    sig[(released) & (df["close"] < lo) & (adx_v > adx_threshold)] = -1
    return sig


# -----------------------------------------------------------------------------
# Strategy 2: Simple grid (adapted from chainstack BTC conservative)
# -----------------------------------------------------------------------------

def signal_grid(
    df: pd.DataFrame,
    n_levels: int = 10,
    range_pct: float = 5.0,
    rebalance_every: int = 96,  # 6h bars in 24 days = ~24d rebalance
) -> pd.Series:
    """Trade the grid: long when price drops N% from rolling midpoint, short when it rises.
    Holds only one position at a time, rebalances every rebalance_every bars."""
    mid = df["close"].rolling(96).mean()  # 4-day rolling midpoint
    sig = pd.Series(0, index=df.index, dtype=int)
    cooldown = 0
    for i in range(96, len(df)):
        if cooldown > 0:
            cooldown -= 1
            continue
        if pd.isna(mid.iloc[i]):
            continue
        rebal = (i % rebalance_every == 0)
        if rebal:
            mid.iloc[i]  # rolling midpoint auto-updates
        threshold = mid.iloc[i] * (range_pct / 100.0)
        # Long when price drops 1 level below midpoint
        if df["close"].iloc[i] < mid.iloc[i] - threshold * 0.5:
            sig.iloc[i] = 1
            cooldown = 24
        elif df["close"].iloc[i] > mid.iloc[i] + threshold * 0.5:
            sig.iloc[i] = -1
            cooldown = 24
    return sig


# -----------------------------------------------------------------------------
# Strategy 4: Funding carry — long when funding<0, short when funding>0
# Data: 1h funding rate per symbol from data/funding_panel.csv
# -----------------------------------------------------------------------------

def signal_funding_carry(
    df: pd.DataFrame,
    funding_col: str = "funding_actual",
    enter_threshold: float = 0.000005,  # enter when |funding| > 5e-6
    exit_threshold: float = 0.0,
) -> pd.Series:
    fund = df[funding_col]
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[fund > enter_threshold] = -1  # shorts pay longs -> go short to collect
    sig[fund < -enter_threshold] = 1   # longs pay shorts -> go long to collect
    # Exit when funding crosses zero
    sig[(fund.abs() < exit_threshold) & (sig != 0)] = 0
    return sig


def signal_funding_max_fade(
    df: pd.DataFrame,
    funding_col: str = "funding_actual",
    max_rate: float = 0.000012,
) -> pd.Series:
    """Short when funding is at max rate (capped). Funding-positive = shorts paying longs, fade the squeeze."""
    fund = df[funding_col]
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[fund >= max_rate * 0.95] = -1  # within 5% of max rate
    sig[fund < max_rate * 0.5] = 0     # exit when funding drops to half max
    return sig


def signal_funding_neg_fade(
    df: pd.DataFrame,
    funding_col: str = "funding_actual",
    neg_threshold: float = -0.000005,
) -> pd.Series:
    """Long when funding is negative (longs paying shorts, contrarian)."""
    fund = df[funding_col]
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[fund <= neg_threshold] = 1
    sig[fund > 0] = 0
    return sig


# -----------------------------------------------------------------------------
# Strategy: Inter-asset funding spread (the "true" funding arb)
# At each bar: long the symbol with LOWEST funding, short the symbol with HIGHEST funding.
# Delta-neutral within HL pure-perp (both legs are perps).
# -----------------------------------------------------------------------------

def signal_inter_asset_funding_spread(
    df: pd.DataFrame,
    long_threshold_bps: float = 2.0,   # 2 bps/hr minimum spread to enter
    symbols: list = None,
) -> pd.Series:
    """Return a series of dicts {symbol: side} per bar, but as DataFrame-friendly format
    we'll instead return a signal for the SYMBOL side, with a separate positions dataframe.
    For simplicity, we encode the long/short in two columns: 'long_sym' and 'short_sym'.
    """
    # This strategy needs special handling — we return a DataFrame with (long_sym, short_sym)
    # columns instead of a single signal Series. The runner will check.
    if symbols is None:
        # find fund_* columns
        symbols = [c.replace("fund_", "") for c in df.columns if c.startswith("fund_")]
    rows = []
    for ts, row in df.iterrows():
        fund_map = {s: row.get(f"fund_{s}", 0) for s in symbols}
        if not fund_map:
            rows.append({"long_sym": None, "short_sym": None})
            continue
        sorted_f = sorted(fund_map.items(), key=lambda x: x[1])
        long_sym = sorted_f[0][0]
        short_sym = sorted_f[-1][0]
        spread = fund_map[short_sym] - fund_map[long_sym]
        if spread * 10000 >= long_threshold_bps:  # 1 unit of funding = 100 bps/hr? no, funding is decimal so 1e-5 = 0.01 bps/hr
            # 1e-5 = 0.01 bps. So 2 bps = 2e-7. Convert: spread * 10000 = bps/hr? No.
            # funding is in decimal. 1.25e-5 = 0.00125% = 0.0125 bps. So 2 bps = 2/0.0125 * 1.25e-5 = 0.002 = 2e-3
            # Actually 1 bp = 0.0001 in decimal. So 2 bps = 0.0002.
            threshold = long_threshold_bps * 0.0001
            if spread >= threshold:
                rows.append({"long_sym": long_sym, "short_sym": short_sym})
            else:
                rows.append({"long_sym": None, "short_sym": None})
        else:
            rows.append({"long_sym": None, "short_sym": None})
    return pd.DataFrame(rows, index=df.index)

def signal_funding_arb(df: pd.DataFrame) -> pd.Series:
    raise NotImplementedError("needs funding data; not run in this loop")


# -----------------------------------------------------------------------------
# Strategy 4: EMA cross baseline (canonical momentum strategy)
# -----------------------------------------------------------------------------

def signal_ma_cross(df: pd.DataFrame, fast: int = 12, slow: int = 26) -> pd.Series:
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    cross_up = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
    cross_dn = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[cross_up] = 1
    sig[cross_dn] = -1
    return sig


# -----------------------------------------------------------------------------
# Data loaders
# -----------------------------------------------------------------------------

def load_moondev_btc_6h() -> tuple[pd.DataFrame, str]:
    p = IMPORTS_DIR / "moondev2" / "backtest" / "data" / "BTC-6h-1000wks-data.csv"
    if not p.exists():
        raise FileNotFoundError(p)
    df = pd.read_csv(p, parse_dates=["datetime"], index_col="datetime")
    df.columns = [c.lower() for c in df.columns]
    return df, "BTC-6h (moondev 1000wk historical)"


def load_moondev_csv(filename: str, label: str) -> tuple[pd.DataFrame, str]:
    p = IMPORTS_DIR / "moondev2" / "backtest" / "data" / filename
    if not p.exists():
        raise FileNotFoundError(p)
    df = pd.read_csv(p, parse_dates=["datetime"], index_col="datetime")
    df.columns = [c.lower() for c in df.columns]
    return df, label


def load_hl_with_funding(symbol: str) -> tuple[pd.DataFrame, str]:
    """Load HL hourly candle + funding merged panel for one symbol."""
    candle_p = PROJECT_ROOT / "data" / "candle_panel.csv"
    fund_p = PROJECT_ROOT / "data" / "funding_panel.csv"
    if not candle_p.exists() or not fund_p.exists():
        raise FileNotFoundError("run scripts/build_candle_panel.py and build_funding_panel.py first")
    c = pd.read_csv(candle_p, parse_dates=["ts"])
    f = pd.read_csv(fund_p, parse_dates=["ts"])
    # Strip timezone to make both tz-naive for join
    c["ts"] = pd.to_datetime(c["ts"]).dt.tz_localize(None)
    f["ts"] = pd.to_datetime(f["ts"]).dt.tz_localize(None)
    c = c[c["symbol"] == symbol].sort_values("ts").set_index("ts")
    f = f[f["symbol"] == symbol].sort_values("ts").set_index("ts")
    df = c[["open", "high", "low", "close", "volume"]].join(
        f[["funding_actual", "funding_predicted"]], how="left"
    )
    df = df.dropna(subset=["close"])
    df = df.fillna({"funding_actual": 0, "funding_predicted": 0})
    return df, f"{symbol}-1h (HL live + funding, {len(df)} bars)"


def load_hl_funding_panel_wide(exclude: list = None) -> tuple[pd.DataFrame, str]:
    """Load HL funding panel as wide (one column per symbol) + close prices for each symbol."""
    candle_p = PROJECT_ROOT / "data" / "candle_panel.csv"
    fund_p = PROJECT_ROOT / "data" / "funding_panel.csv"
    if not candle_p.exists() or not fund_p.exists():
        raise FileNotFoundError("run scripts/build_candle_panel.py and build_funding_panel.py first")
    c = pd.read_csv(candle_p, parse_dates=["ts"])
    f = pd.read_csv(fund_p, parse_dates=["ts"])
    c["ts"] = pd.to_datetime(c["ts"]).dt.tz_localize(None)
    f["ts"] = pd.to_datetime(f["ts"]).dt.tz_localize(None)
    # Exclude symbols with bad data (e.g., HIP-3 mixed GOLD/SILVER or sparse symbols)
    if exclude is None:
        exclude = []
    keep = [s for s in c["symbol"].unique() if s not in exclude]
    c = c[c["symbol"].isin(keep)]
    f = f[f["symbol"].isin(keep)]
    # Pivot funding: rows = ts, cols = symbols
    fund_pivot = f.pivot_table(index="ts", columns="symbol", values="funding_actual", aggfunc="last")
    fund_pivot.columns = [f"fund_{c}" for c in fund_pivot.columns]
    # Pivot close: rows = ts, cols = symbols
    close_pivot = c.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last")
    close_pivot.columns = [f"px_{c}" for c in close_pivot.columns]
    # Pivot high/low for the high/low of each symbol (needed for backtest)
    high_pivot = c.pivot_table(index="ts", columns="symbol", values="high", aggfunc="max")
    high_pivot.columns = [f"hi_{c}" for c in high_pivot.columns]
    low_pivot = c.pivot_table(index="ts", columns="symbol", values="low", aggfunc="min")
    low_pivot.columns = [f"lo_{c}" for c in low_pivot.columns]
    df = fund_pivot.join(close_pivot, how="inner").join(high_pivot, how="inner").join(low_pivot, how="inner")
    df = df.dropna()
    return df, f"inter-asset (HL live, {len(df)} hours, {len(fund_pivot.columns)} symbols, excluded={exclude})"


def load_hl_recent(symbol: str = "ETH", tf: str = "1h") -> tuple[pd.DataFrame, str]:
    """Load HL recent candles from our ws_candle data or fetch via API."""
    # First try local ws_candle data
    files = list((PROJECT_ROOT / "data" / "ws_candle").glob(f"{symbol}_*.jsonl")) if (PROJECT_ROOT / "data" / "ws_candle").exists() else []
    if not files:
        # Fall back to API
        import requests
        end = int(pd.Timestamp.utcnow().timestamp() * 1000)
        start = end - (90 * 24 * 3600 * 1000)  # 90 days
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        iv = interval_map.get(tf, "1h")
        try:
            r = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "candleSnapshot", "req": {"coin": symbol, "interval": iv, "startTime": start, "endTime": end}},
                timeout=10,
            )
            data = r.json()
            if not data:
                raise RuntimeError(f"empty response: {r.text[:200]}")
            rows = [{"datetime": pd.to_datetime(c["t"], unit="ms"), "open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"])} for c in data]
            df = pd.DataFrame(rows).set_index("datetime").sort_index()
            return df, f"{symbol}-{tf} (HL API 90d)"
        except Exception as e:
            raise FileNotFoundError(f"no local candle data and API failed: {e}")
    # Concat local files
    dfs = []
    for f in files:
        try:
            tmp = pd.read_json(f, lines=True)
            if "t" in tmp.columns:
                tmp["datetime"] = pd.to_datetime(tmp["t"], unit="ms")
                tmp = tmp.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
                tmp = tmp[["datetime", "open", "high", "low", "close", "volume"]].set_index("datetime")
            dfs.append(tmp)
        except Exception:
            continue
    if not dfs:
        raise RuntimeError("no parseable candle files")
    df = pd.concat(dfs).sort_index()
    # Resample to requested timeframe if needed
    if tf != "raw":
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        df = df.resample(tf).agg(agg).dropna()
    return df, f"{symbol}-{tf} (HL local candle data)"


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

STRATEGIES = {
    "bb_squeeze": {
        "name": "BB Squeeze + ADX (moondev Harvard)",
        "signal_fn": signal_bb_squeeze_adx,
        "params": {"bb_window": 20, "bb_std": 2.0, "keltner_window": 20, "keltner_atr_mult": 1.5, "adx_period": 14, "adx_threshold": 25.0},
        "take_profit": 0.05,
        "stop_loss": 0.03,
        "max_hold": 168,  # 168 * 6h = 42 days
    },
    "grid": {
        "name": "Rolling Grid (chainstack conservative)",
        "signal_fn": signal_grid,
        "params": {"n_levels": 10, "range_pct": 5.0, "rebalance_every": 96},
        "take_profit": 0.025,
        "stop_loss": 0.025,
        "max_hold": 24,
    },
    "ma_cross": {
        "name": "EMA Cross 12/26 (baseline momentum)",
        "signal_fn": signal_ma_cross,
        "params": {"fast": 12, "slow": 26},
        "take_profit": 0.05,
        "stop_loss": 0.03,
        "max_hold": 168,
    },
    "funding_carry": {
        "name": "Funding carry (sign of funding)",
        "signal_fn": signal_funding_carry,
        "params": {"funding_col": "funding_actual", "enter_threshold": 0.000005, "exit_threshold": 0.0},
        "take_profit": 0.01,
        "stop_loss": 0.01,
        "max_hold": 24,
        "requires_funding": True,
    },
    "funding_max_fade": {
        "name": "Funding max-rate fade (short when fund=cap)",
        "signal_fn": signal_funding_max_fade,
        "params": {"funding_col": "funding_actual", "max_rate": 0.000012},
        "take_profit": 0.02,
        "stop_loss": 0.02,
        "max_hold": 24,
        "requires_funding": True,
    },
    "funding_neg_fade": {
        "name": "Funding negative fade (long when fund<0)",
        "signal_fn": signal_funding_neg_fade,
        "params": {"funding_col": "funding_actual", "neg_threshold": -0.000005},
        "take_profit": 0.01,
        "stop_loss": 0.01,
        "max_hold": 24,
        "requires_funding": True,
    },
}

# Inter-asset funding spread has its own dedicated runner (2-leg backtest)
INTER_ASSET_STRATEGIES = {
    "inter_asset_spread": {
        "name": "Inter-asset funding spread (long low-fund, short high-fund)",
        "threshold_bps_per_hr": 0.5,
        "max_hold": 24,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.02,
    },
    "inter_asset_spread_wide": {
        "name": "Inter-asset funding spread (1 bps/hr threshold)",
        "threshold_bps_per_hr": 1.0,
        "max_hold": 48,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.03,
    },
}


def run_one(name: str, df: pd.DataFrame, sym_tf: str) -> StrategyResult:
    spec = STRATEGIES[name]
    sig = spec["signal_fn"](df, **spec["params"])
    out = backtest_long_short(
        df, sig,
        take_profit=spec["take_profit"],
        stop_loss=spec["stop_loss"],
        max_hold=spec.get("max_hold"),
        cost_bps=8.0,
        initial_equity=10_000.0,
        risk_per_trade=0.01,
    )
    s = score(out["trades"], out["equity_curve"], out["n_bars"])
    return StrategyResult(
        name=spec["name"],
        symbol=sym_tf,
        timeframe=sym_tf,
        n_bars=out["n_bars"],
        **s,
        start=str(df.index[0]),
        end=str(df.index[-1]),
        raw_equity_tail=[float(x) for x in out["equity_curve"][-20:]],
    )


def run_inter_asset(spec: dict, df: pd.DataFrame, sym_tf: str) -> StrategyResult:
    out = backtest_inter_asset_funding_spread(
        df,
        threshold_bps_per_hr=spec.get("threshold_bps_per_hr", 0.5),
        max_hold=spec.get("max_hold", 24),
        stop_loss_pct=spec.get("stop_loss_pct", 0.03),
        take_profit_pct=spec.get("take_profit_pct", 0.02),
        cost_bps_per_leg=4.0,
        notional_per_leg=5_000.0,
        initial_cash=10_000.0,
    )
    s = score_2leg(out["trades"], out["equity_curve"], out["n_bars"])
    return StrategyResult(
        name=spec["name"],
        symbol=sym_tf,
        timeframe=sym_tf,
        n_bars=out["n_bars"],
        **s,
        start=str(df.index[0]),
        end=str(df.index[-1]),
        raw_equity_tail=[float(x) for x in out["equity_curve"][-20:]],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=list(STRATEGIES.keys()) + ["all"], default="all")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--tf", default="6h")
    ap.add_argument("--source", choices=["moondev-btc6h", "moondev-btc1h", "moondev-eth1d", "moondev-sol1d", "hl-funding", "hl-inter-asset", "hl-recent"], default="moondev-btc6h")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    if args.source == "moondev-btc6h":
        df, src_label = load_moondev_btc_6h()
    elif args.source == "moondev-btc1h":
        df, src_label = load_moondev_csv("BTC-1h-1000wks-data.csv", "BTC-1h (moondev 1000wk)")
    elif args.source == "moondev-eth1d":
        df, src_label = load_moondev_csv("ETH-1d-1000wks-data.csv", "ETH-1d (moondev 1000wk)")
    elif args.source == "moondev-sol1d":
        df, src_label = load_moondev_csv("SOL-1d-1000wks-data.csv", "SOL-1d (moondev 1000wk)")
    elif args.source == "hl-funding":
        df, src_label = load_hl_with_funding(args.symbol)
    elif args.source == "hl-inter-asset":
        df, src_label = load_hl_funding_panel_wide(exclude=["XYZ", "DOGE"])
    else:
        df, src_label = load_hl_recent(args.symbol, args.tf)

    print(f"=== data: {src_label} ===", flush=True)
    print(f"  rows: {len(df)}  range: {df.index[0]} -> {df.index[-1]}", flush=True)
    if "close" in df.columns:
        close_start = df['close'].iloc[0]
        close_end = df['close'].iloc[-1]
        ret_pct = (close_end / close_start - 1) * 100
        print(f"  close: ${close_start:.2f} -> ${close_end:.2f}  ({ret_pct:+.1f}%)", flush=True)
    else:
        px_cols = [c for c in df.columns if c.startswith("px_")]
        if px_cols:
            sym = px_cols[0].replace("px_", "")
            close_start = df[px_cols[0]].iloc[0]
            close_end = df[px_cols[0]].iloc[-1]
            ret_pct = (close_end / close_start - 1) * 100
            print(f"  {sym} px: {close_start:.4f} -> {close_end:.4f}  ({ret_pct:+.1f}%)", flush=True)
    print(flush=True)

    strats = [args.strategy] if args.strategy != "all" else list(STRATEGIES.keys()) + list(INTER_ASSET_STRATEGIES.keys())
    results: list[StrategyResult] = []
    for name in strats:
        if name in INTER_ASSET_STRATEGIES:
            spec = INTER_ASSET_STRATEGIES[name]
            print(f"=== running {name} (inter-asset 2-leg) ===", flush=True)
            try:
                r = run_inter_asset(spec, df, args.symbol)
                results.append(r)
                print(f"  n={r.n_trades}  WR={r.win_rate:.1f}%  PF={r.profit_factor:.2f}  med={r.median_return_pct:+.4f}%  ret={r.net_return_pct:+.2f}%  maxDD={r.max_drawdown_pct:.1f}%  total_funding=${r.total_funding_usd:.2f}", flush=True)
            except Exception as e:
                print(f"  FAILED: {e}", flush=True)
            continue
        spec = STRATEGIES.get(name, {})
        if spec.get("requires_funding") and "funding_actual" not in df.columns:
            print(f"=== skipping {name} (no funding data in this source) ===", flush=True)
            continue
        print(f"=== running {name} ===", flush=True)
        try:
            r = run_one(name, df, f"{args.symbol}-{args.tf}")
            results.append(r)
            print(f"  n={r.n_trades}  WR={r.win_rate:.1f}%  PF={r.profit_factor:.2f}  med={r.median_return_pct:+.4f}%  ret={r.net_return_pct:+.2f}%  maxDD={r.max_drawdown_pct:.1f}%  Sharpe={r.sharpe:.2f}", flush=True)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
    print(flush=True)

    # Save results
    out_file = RESULTS_DIR / f"search_{pd.Timestamp.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps([asdict(r) for r in results], indent=2, default=str))
    print(f"saved -> {out_file}", flush=True)


if __name__ == "__main__":
    main()

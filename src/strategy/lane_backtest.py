"""Lane-aware backtests for HyphyLiquid research tracks.

This module keeps BTC/ETH execution research separate from alt research.
The first alt lane is a condor-inspired range scalp: liquidation burst at a
Bollinger extreme, confirmation back inside the band, then fade toward the
mid-band with a stop beyond the band.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import mean, median, pstdev
from typing import Iterable

from src.strategy.fade_or_follow_backtest import (
    _bar_dt,
    _bar_ts,
    _fade_direction,
    _return_pct,
    find_entry_idx,
)

ALT_RESEARCH_SYMBOLS = {"SOL", "HYPE", "DOGE", "BNB"}
BAND_WIDTH_BUCKETS = (
    ("compressed", 0.5),
    ("normal", 1.0),
    ("wide", 2.0),
)


@dataclass
class LaneTrade:
    """One simulated lane-backtest trade."""

    lane: str
    cascade_start_ts: str
    symbol: str
    side: str
    direction: str
    entry_ts: str
    entry_price: float
    exit_ts: str
    exit_price: float
    gross_return_pct: float
    net_return_pct: float
    bars_held: int
    entry_lag_s: float
    band_mid: float
    band_upper: float
    band_lower: float
    band_width_pct: float
    exit_reason: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _close(bar: dict) -> float | None:
    try:
        return float(bar.get("c") or bar.get("payload", {}).get("c"))
    except (TypeError, ValueError):
        return None


def _high(bar: dict) -> float | None:
    try:
        return float(bar.get("h") or bar.get("payload", {}).get("h"))
    except (TypeError, ValueError):
        return None


def _low(bar: dict) -> float | None:
    try:
        return float(bar.get("l") or bar.get("payload", {}).get("l"))
    except (TypeError, ValueError):
        return None


def bollinger_at(
    candles: list[dict],
    idx: int,
    period: int = 20,
    stdev_mult: float = 2.0,
) -> dict | None:
    """Return Bollinger bands using candles before idx.

    The current signal bar is excluded so the backtest does not use its close
    to move the band it is testing against.
    """
    start = idx - period
    if start < 0:
        return None
    closes = [_close(c) for c in candles[start:idx]]
    if any(c is None for c in closes):
        return None
    vals = [float(c) for c in closes if c is not None]
    mid = mean(vals)
    sigma = pstdev(vals)
    upper = mid + stdev_mult * sigma
    lower = mid - stdev_mult * sigma
    width_pct = ((upper - lower) / mid * 100.0) if mid > 0 else 0.0
    return {
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "width_pct": width_pct,
    }


def _range_confirmation(side: str, bar: dict, bands: dict) -> bool:
    close = _close(bar)
    high = _high(bar)
    low = _low(bar)
    if close is None or high is None or low is None:
        return False
    if side == "B":
        return high >= bands["upper"] and close < bands["upper"]
    if side == "A":
        return low <= bands["lower"] and close > bands["lower"]
    return False


def _exit_for_range_trade(
    candles: list[dict],
    entry_idx: int,
    direction: str,
    bands: dict,
    max_hold_minutes: int,
    stop_buffer_bps: float,
) -> tuple[int, float, str] | None:
    if entry_idx >= len(candles):
        return None
    stop_buffer = stop_buffer_bps / 10_000.0
    if direction == "long":
        target = bands["mid"]
        stop = bands["lower"] * (1.0 - stop_buffer)
    else:
        target = bands["mid"]
        stop = bands["upper"] * (1.0 + stop_buffer)

    last_idx = min(entry_idx + max_hold_minutes, len(candles) - 1)
    for i in range(entry_idx + 1, last_idx + 1):
        high = _high(candles[i])
        low = _low(candles[i])
        close = _close(candles[i])
        if high is None or low is None or close is None:
            continue
        # Conservative same-bar ordering: stop beats target.
        if direction == "long":
            if low <= stop:
                return i, stop, "stop"
            if high >= target:
                return i, target, "mid_band_target"
        else:
            if high >= stop:
                return i, stop, "stop"
            if low <= target:
                return i, target, "mid_band_target"
    close = _close(candles[last_idx])
    if close is None:
        return None
    return last_idx, close, "timeout"


def run_alt_range_liq_scalp(
    cascades: list[dict],
    candles_by_symbol: dict[str, list[dict]],
    *,
    symbols: set[str] | None = None,
    band_period: int = 20,
    stdev_mult: float = 2.0,
    max_band_width_pct: float | None = None,
    max_hold_minutes: int = 15,
    max_entry_lag_minutes: int | None = 2,
    stop_buffer_bps: float = 5.0,
    round_trip_cost_bps: float = 8.0,
) -> list[LaneTrade]:
    """Run the alt range/liquidation scalp lane.

    Args:
        cascades: Canonical cascades from data/cascades.jsonl.
        candles_by_symbol: 1m candles keyed by symbol.
        symbols: Symbols to allow. Defaults to research-only alt symbols.
        band_period: Number of prior 1m closes used for Bollinger bands.
        stdev_mult: Bollinger standard-deviation multiplier.
        max_band_width_pct: Optional compression filter. If set, skip when
            band width is wider than this percent of mid.
        max_hold_minutes: Timeout after entry.
        max_entry_lag_minutes: Skip if the first eligible candle is too late.
        stop_buffer_bps: Stop beyond the outer band.
        round_trip_cost_bps: Fees + spread/slippage haircut applied to return.
    """
    allowed = {s.upper() for s in (symbols or ALT_RESEARCH_SYMBOLS)}
    trades: list[LaneTrade] = []
    for cascade in cascades:
        sym = str(cascade.get("symbol", "")).upper()
        side = cascade.get("side")
        start_ts = cascade.get("start_ts")
        if sym not in allowed or side not in {"A", "B"} or not start_ts:
            continue
        candles = candles_by_symbol.get(sym)
        if not candles:
            continue
        entry_idx = find_entry_idx(candles, start_ts, max_entry_lag_minutes)
        if entry_idx is None:
            continue
        bands = bollinger_at(candles, entry_idx, band_period, stdev_mult)
        if bands is None:
            continue
        if max_band_width_pct is not None and bands["width_pct"] > max_band_width_pct:
            continue
        entry_bar = candles[entry_idx]
        if not _range_confirmation(side, entry_bar, bands):
            continue
        entry_px = _close(entry_bar)
        if entry_px is None or entry_px <= 0:
            continue
        direction = _fade_direction(side)
        exit_tuple = _exit_for_range_trade(
            candles,
            entry_idx,
            direction,
            bands,
            max_hold_minutes,
            stop_buffer_bps,
        )
        if exit_tuple is None:
            continue
        exit_idx, exit_px, exit_reason = exit_tuple
        gross = _return_pct(direction, entry_px, exit_px)
        net = gross - (round_trip_cost_bps / 100.0)
        entry_dt = _bar_dt(entry_bar)
        cascade_dt = datetime.fromisoformat(start_ts)
        if cascade_dt.tzinfo is None:
            cascade_dt = cascade_dt.replace(tzinfo=timezone.utc)
        entry_lag_s = (entry_dt - cascade_dt).total_seconds() if entry_dt else 0.0
        trades.append(
            LaneTrade(
                lane="alt_range_liq_scalp",
                cascade_start_ts=start_ts,
                symbol=sym,
                side=side,
                direction=direction,
                entry_ts=_bar_ts(entry_bar),
                entry_price=round(entry_px, 8),
                exit_ts=_bar_ts(candles[exit_idx]),
                exit_price=round(exit_px, 8),
                gross_return_pct=round(gross, 4),
                net_return_pct=round(net, 4),
                bars_held=max(0, exit_idx - entry_idx),
                entry_lag_s=round(entry_lag_s, 3),
                band_mid=round(bands["mid"], 8),
                band_upper=round(bands["upper"], 8),
                band_lower=round(bands["lower"], 8),
                band_width_pct=round(bands["width_pct"], 4),
                exit_reason=exit_reason,
                reason="liquidation burst at band extreme with close back inside",
            )
        )
    return trades


def summarize_lane_trades(trades: Iterable[LaneTrade]) -> dict:
    """Summarize lane trades by lane and symbol."""
    by_key: dict[tuple[str, str], list[LaneTrade]] = {}
    for trade in trades:
        by_key.setdefault((trade.lane, trade.symbol), []).append(trade)

    summary = {}
    for (lane, sym), rows in by_key.items():
        returns = [t.net_return_pct for t in rows]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses) if losses else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        summary[f"{lane}|{sym}"] = {
            "lane": lane,
            "symbol": sym,
            "n": len(rows),
            "win_rate": round(len(wins) / len(rows), 4) if rows else 0.0,
            "avg_net_return_pct": round(mean(returns), 4) if returns else 0.0,
            "median_net_return_pct": round(median(returns), 4) if returns else 0.0,
            "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            "min_net_return_pct": round(min(returns), 4) if returns else 0.0,
            "max_net_return_pct": round(max(returns), 4) if returns else 0.0,
        }
    return summary


def _band_width_bucket(width_pct: float) -> str:
    for label, upper in BAND_WIDTH_BUCKETS:
        if width_pct <= upper:
            return label
    return "very_wide"


def _summarize_returns(rows: list[dict], return_field: str) -> dict:
    returns = [float(r[return_field]) for r in rows]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses) if losses else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    largest_win = max(wins) if wins else 0.0
    return {
        "n": len(rows),
        "win_rate": round(len(wins) / len(rows), 4) if rows else 0.0,
        "avg_return_pct": round(mean(returns), 4) if returns else 0.0,
        "median_return_pct": round(median(returns), 4) if returns else 0.0,
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "largest_win_pct": round(largest_win, 4),
        "largest_win_share_of_gross_profit": (
            round(largest_win / gross_profit, 4) if gross_profit > 0 else 0.0
        ),
    }


def diagnostic_breakdown(
    trades: Iterable[dict],
    *,
    return_field: str,
    include_band_buckets: bool = False,
) -> dict:
    """Return side/regime diagnostics for serialized lane trades.

    Args:
        trades: Serialized trade dictionaries.
        return_field: Return column to evaluate, e.g. `return_pct` or
            `net_return_pct`.
        include_band_buckets: Include `band_width_pct` bucket diagnostics.
    """
    rows = [dict(t) for t in trades if return_field in t]
    out: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {"all": rows}
    for row in rows:
        symbol = row.get("symbol", "?")
        side = row.get("side", "?")
        direction = row.get("direction", "?")
        exit_reason = row.get("exit_reason", "fixed_horizon")
        groups.setdefault(f"symbol={symbol}", []).append(row)
        groups.setdefault(f"side={side}", []).append(row)
        groups.setdefault(f"symbol={symbol}|side={side}", []).append(row)
        groups.setdefault(f"direction={direction}", []).append(row)
        groups.setdefault(f"exit_reason={exit_reason}", []).append(row)
        if include_band_buckets and row.get("band_width_pct") is not None:
            try:
                bucket = _band_width_bucket(float(row["band_width_pct"]))
            except (TypeError, ValueError):
                bucket = "unknown"
            groups.setdefault(f"band_width={bucket}", []).append(row)
            groups.setdefault(f"symbol={symbol}|band_width={bucket}", []).append(row)

    for key, grouped_rows in sorted(groups.items()):
        if not grouped_rows:
            continue
        out[key] = _summarize_returns(grouped_rows, return_field)
    return out

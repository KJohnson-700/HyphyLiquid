"""
HyphyLiquid - first feature-backed backtest for the BTC/ETH
liquidation_fade_or_follow strategy.

For each canonical cascade (from data/cascades.jsonl), runs three
variants on the 1m candle feed and reports per-trade outcomes:

  baseline_fade
    Enter FADE on the next completed 1m bar. No response filter.
    Control: what the original "always fade" rule would have done.

  reclaim_fade
    Wait up to `wait_minutes` for a reclaim signal. A reclaim is
    direction-dependent:
      B-side cascade (price went up via short liquidations):
        reclaim = any 1m bar close in the window < event_vwap
      A-side cascade (price went down via long liquidations):
        reclaim = any 1m bar close in the window > event_vwap
    If reclaim detected, enter FADE at the bar that triggered it.

  failed_reclaim_continuation
    Wait the same `wait_minutes`. If NO reclaim in the window,
    enter CONTINUATION at the end of the window. Continuation is
    with-the-cascade direction.

Pinned rules (per the spec build order):
  Reclaim definition (pinned by Slim):
    Bullish: close > event_VWAP on the next completed 1m bar
    Bearish: close < event_VWAP on the next completed 1m bar
  In this code, "reclaim" means the bar(s) that demonstrate the
  initial move has reversed (B-side -> close < vwap, A-side ->
  close > vwap), so a fade is justified. The same logic is the
  inverse for the failed-reclaim continuation entry.
  Add a buffer later if too noisy (5-10 bps) - not yet.

For each trade, the backtest records:
  direction (long/short), entry_bar, entry_price, exit_bar,
  exit_price, return_pct, bars_held, reclaim_detected, reason
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Iterable


@dataclass
class Trade:
    cascade_start_ts: str
    symbol: str
    side: str
    variant: str
    direction: str
    event_vwap: float
    entry_ts: str
    entry_price: float
    exit_ts: str
    exit_price: float
    return_pct: float
    bars_held: int
    entry_lag_s: float
    reclaim_detected: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_reclaim(side: str, close: float, vwap: float) -> bool:
    """B-side cascade moved price up; reclaim = close < vwap.
    A-side cascade moved price down; reclaim = close > vwap."""
    if side == "B":
        return close < vwap
    if side == "A":
        return close > vwap
    return False


def _fade_direction(side: str) -> str:
    return "short" if side == "B" else "long"


def _continuation_direction(side: str) -> str:
    return "long" if side == "B" else "short"


def _return_pct(direction: str, entry: float, exit: float) -> float:
    if entry <= 0:
        return 0.0
    if direction == "long":
        return (exit - entry) / entry * 100.0
    if direction == "short":
        return (entry - exit) / entry * 100.0
    return 0.0


def _bars_held(entry_idx: int, exit_idx: int) -> int:
    return max(0, exit_idx - entry_idx)


def _bar_dt(bar: dict) -> datetime | None:
    t = bar.get("t")
    if t is None:
        t = bar.get("payload", {}).get("t")
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(int(t) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def find_entry_idx(
    candles: list[dict],
    cascade_start_ts: str,
    max_entry_lag_minutes: int | None = 2,
) -> int | None:
    """Index of the first 1m bar after the cascade start.

    If candle capture starts after the event, skip instead of entering
    minutes or hours late. This prevents backtests from using unrelated
    later candles for early cascades.
    """
    cs = _parse_ts(cascade_start_ts)
    for i, c in enumerate(candles):
        bar_open = _bar_dt(c)
        if bar_open is None:
            continue
        if bar_open > cs:
            if max_entry_lag_minutes is not None:
                lag_s = (bar_open - cs).total_seconds()
                if lag_s > max_entry_lag_minutes * 60:
                    return None
            return i
    return None


def run_backtest(
    cascades: list[dict],
    candles_by_symbol: dict[str, list[dict]],
    horizon_minutes: int = 15,
    wait_minutes: int = 3,
    max_entry_lag_minutes: int | None = 2,
) -> list[Trade]:
    """Run all three variants for each cascade. Returns flat list of trades."""
    out: list[Trade] = []
    for c in cascades:
        sym = c.get("symbol")
        side = c.get("side")
        vwap = float(c.get("event_vwap", 0) or 0)
        start_ts = c.get("start_ts")
        if not (sym and side and vwap > 0 and start_ts):
            continue
        candles = candles_by_symbol.get(sym)
        if not candles:
            continue
        entry_idx = find_entry_idx(candles, start_ts, max_entry_lag_minutes)
        if entry_idx is None:
            continue
        exit_idx_target = entry_idx + horizon_minutes
        if exit_idx_target >= len(candles):
            continue
        entry_bar = candles[entry_idx]
        exit_bar = candles[exit_idx_target]
        try:
            entry_px = float(entry_bar.get("c") or entry_bar.get("payload", {}).get("c"))
            exit_px = float(exit_bar.get("c") or exit_bar.get("payload", {}).get("c"))
        except (TypeError, ValueError):
            continue
        if entry_px <= 0 or exit_px <= 0:
            continue
        entry_lag_s = (_bar_dt(entry_bar) - _parse_ts(start_ts)).total_seconds() if _bar_dt(entry_bar) else 0.0
        # Wait window
        window_end = min(entry_idx + wait_minutes, len(candles) - 1)
        reclaim_detected = False
        reclaim_idx: int | None = None
        for j in range(entry_idx, window_end + 1):
            bar = candles[j]
            try:
                close = float(bar.get("c") or bar.get("payload", {}).get("c"))
            except (TypeError, ValueError):
                continue
            if _is_reclaim(side, close, vwap):
                reclaim_detected = True
                reclaim_idx = j
                break

        # --- Variant 1: baseline_fade ---
        # Enter FADE on the entry bar (bar after cascade). No wait.
        out.append(
            Trade(
                cascade_start_ts=start_ts,
                symbol=sym,
                side=side,
                variant="baseline_fade",
                direction=_fade_direction(side),
                event_vwap=vwap,
                entry_ts=_bar_ts(entry_bar),
                entry_price=entry_px,
                exit_ts=_bar_ts(exit_bar),
                exit_price=exit_px,
                return_pct=round(
                    _return_pct(_fade_direction(side), entry_px, exit_px), 4
                ),
                bars_held=horizon_minutes,
                entry_lag_s=round(entry_lag_s, 3),
                reclaim_detected=reclaim_detected,
                reason="always fade (no filter)",
            )
        )

        # --- Variant 2: reclaim_fade ---
        # Only enter FADE if reclaim detected. Enter at the reclaim bar.
        if reclaim_detected and reclaim_idx is not None:
            reclaim_bar = candles[reclaim_idx]
            try:
                rb_close = float(reclaim_bar.get("c") or reclaim_bar.get("payload", {}).get("c"))
            except (TypeError, ValueError):
                rb_close = 0
            if rb_close > 0:
                # Exit bar is reclaim bar + horizon
                ex_idx = reclaim_idx + horizon_minutes
                if ex_idx < len(candles):
                    ex_bar = candles[ex_idx]
                    try:
                        ex_close = float(ex_bar.get("c") or ex_bar.get("payload", {}).get("c"))
                    except (TypeError, ValueError):
                        ex_close = 0
                    if ex_close > 0:
                        out.append(
                            Trade(
                                cascade_start_ts=start_ts,
                                symbol=sym,
                                side=side,
                                variant="reclaim_fade",
                                direction=_fade_direction(side),
                                event_vwap=vwap,
                                entry_ts=_bar_ts(reclaim_bar),
                                entry_price=rb_close,
                                exit_ts=_bar_ts(ex_bar),
                                exit_price=ex_close,
                                return_pct=round(
                                    _return_pct(_fade_direction(side), rb_close, ex_close), 4
                                ),
                                bars_held=horizon_minutes,
                                entry_lag_s=round(
                                    ((_bar_dt(reclaim_bar) - _parse_ts(start_ts)).total_seconds()
                                     if _bar_dt(reclaim_bar) else 0.0),
                                    3,
                                ),
                                reclaim_detected=True,
                                reason=f"reclaim at bar {reclaim_idx - entry_idx} into wait",
                            )
                        )

        # --- Variant 3: failed_reclaim_continuation ---
        # If NO reclaim in window, enter CONTINUATION at end of window.
        if not reclaim_detected:
            entry_continuation = candles[window_end]
            try:
                ec_px = float(entry_continuation.get("c") or entry_continuation.get("payload", {}).get("c"))
            except (TypeError, ValueError):
                ec_px = 0
            if ec_px > 0:
                ex_idx = window_end + horizon_minutes
                if ex_idx < len(candles):
                    ex_bar = candles[ex_idx]
                    try:
                        ex_close = float(ex_bar.get("c") or ex_bar.get("payload", {}).get("c"))
                    except (TypeError, ValueError):
                        ex_close = 0
                    if ex_close > 0:
                        out.append(
                            Trade(
                                cascade_start_ts=start_ts,
                                symbol=sym,
                                side=side,
                                variant="failed_reclaim_continuation",
                                direction=_continuation_direction(side),
                                event_vwap=vwap,
                                entry_ts=_bar_ts(entry_continuation),
                                entry_price=ec_px,
                                exit_ts=_bar_ts(ex_bar),
                                exit_price=ex_close,
                                return_pct=round(
                                    _return_pct(_continuation_direction(side), ec_px, ex_close), 4
                                ),
                                bars_held=horizon_minutes,
                                entry_lag_s=round(
                                    ((_bar_dt(entry_continuation) - _parse_ts(start_ts)).total_seconds()
                                     if _bar_dt(entry_continuation) else 0.0),
                                    3,
                                ),
                                reclaim_detected=False,
                                reason=f"no reclaim in {wait_minutes}min wait -> continuation",
                            )
                        )
    return out


def _bar_ts(bar: dict) -> str:
    t = bar.get("t")
    if t is None:
        t = bar.get("payload", {}).get("t")
    if t is None:
        return ""
    try:
        return datetime.fromtimestamp(int(t) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def summarize(trades: Iterable[Trade]) -> dict:
    """Per-(variant, symbol) summary: n, win_rate, avg/median return, profit factor."""
    from statistics import mean, median

    by_key: dict[tuple, list[Trade]] = {}
    for t in trades:
        by_key.setdefault((t.variant, t.symbol), []).append(t)

    summary = {}
    for (variant, sym), ts in by_key.items():
        rets = [t.return_pct for t in ts]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses) if losses else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        summary[f"{variant}|{sym}"] = {
            "variant": variant,
            "symbol": sym,
            "n": len(ts),
            "win_rate": round(len(wins) / len(ts), 4) if ts else 0.0,
            "avg_return_pct": round(mean(rets), 4) if rets else 0.0,
            "median_return_pct": round(median(rets), 4) if rets else 0.0,
            "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            "min_return": round(min(rets), 4) if rets else 0.0,
            "max_return": round(max(rets), 4) if rets else 0.0,
        }
    return summary

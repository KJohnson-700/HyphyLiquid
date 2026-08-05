"""Relative-value / dislocation backtest (research only).

For each alt liquidation cascade event (SOL, DOGE, BNB, xyz:SILVER,
optional xyz:GOLD), compute forward returns on the alt AND on BTC/ETH
over the same windows, plus a regime context (rolling beta, deviation
from expected beta-move, funding bucket, OI direction, BTC/ETH
calm/trending, isolation from BTC/ETH cascades). Then test three
playbook filters (H1: BTC/ETH calm, H2: BTC/ETH do NOT confirm the alt
direction, H3: isolated vs generic) and report per-symbol verdicts
against the promotion gate.

HARD SCOPE:
  - Research only. Does NOT touch execution, order_manager, risk.py,
    or any live/paper routing.
  - Writes 3 outputs only:
      data/relative_value_dislocation_results.json
      data/relative_value_dislocation_summary.md
      vault/research/2026-08-05-RELATIVE-VALUE-DISLOCATION-BACKTEST.md
    (vault note is a separate write that the operator runs after the
    script; this script only emits the first two + structured JSON).

Promotion gate (per symbol, per playbook, per horizon):
  n >= 30, PF > 1.5, median_return_pct > 0, top_win_share <= 35%

Run:
    python scripts/run_relative_value_dislocation.py
    python scripts/run_relative_value_dislocation.py --horizons 15,30
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.strategy.event_features import _canonical_symbol, _file_stem  # noqa: E402


# ----------------------------- constants ---------------------------------- #

# Per Slim's spec (2026-08-05). HYPE intentionally excluded.
ALT_SYMBOLS: tuple[str, ...] = ("SOL", "DOGE", "BNB", "xyz:SILVER", "xyz:GOLD")
REF_SYMBOLS: tuple[str, ...] = ("BTC", "ETH")
ALL_SYMBOLS: tuple[str, ...] = ALT_SYMBOLS + REF_SYMBOLS

# Forward-return windows in minutes.
DEFAULT_HORIZONS: tuple[int, ...] = (5, 15, 30, 60)

# Window for rolling beta (minutes of 1m returns before the event).
BETA_WINDOW_MIN: int = 60

# Window for realized vol pre-event (BTC/ETH "calm/trending" bucket).
VOL_WINDOW_MIN: int = 30

# Window for "isolated" check: no BTC/ETH cascade in this many minutes
# immediately before the alt event.
ISOLATION_WINDOW_MIN: int = 30

# Funding neutral band (fractional, per hour). |funding| < this -> neutral.
FUNDING_NEUTRAL_BAND: float = 0.0001  # 1 bp/hr

# OI direction threshold (fractional). |delta_oi / oi_then| < this -> flat.
OI_DIRECTION_THRESHOLD_PCT: float = 0.01  # 1%

# Confirm threshold: both ref and alt must move more than this (in percent)
# in the same direction for the ref to "confirm" the alt cascade.
CONFIRM_MIN_RETURN_PCT: float = 0.05  # 0.05%

# Calm threshold: realized vol (1m returns) below this fraction counts as calm.
# We compute it dynamically per run as the median across events (so the bucket
# is relative to the current sample). This is the floor.
CALM_VOL_THRESHOLD: float = 1e-6  # 1 bp / minute stdev = essentially flat

# Promotion gate.
PROMOTION_N: int = 30
PROMOTION_PF: float = 1.5
PROMOTION_TOP_WIN_SHARE: float = 0.35  # 35%

# Paths.
CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
CANDLE_DIR = REPO_ROOT / "data" / "ws_candle"
ASSET_CTX_DIR = REPO_ROOT / "data" / "asset_ctx"
RESULTS_JSON_PATH = REPO_ROOT / "data" / "relative_value_dislocation_results.json"
SUMMARY_MD_PATH = REPO_ROOT / "data" / "relative_value_dislocation_summary.md"


# ----------------------------- dataclasses -------------------------------- #


@dataclass
class PromotionVerdict:
    symbol: str
    playbook: str
    horizon_minutes: int
    n: int
    win_rate: float
    avg_pnl_pct: float
    median_pnl_pct: float
    pf: float
    top_win_share: float
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    passed: bool = False
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------- pure math helpers ---------------------------- #


def _forward_return(
    candles: list[dict], entry_ts_ms: int, exit_ts_ms: int
) -> float | None:
    """Compute (exit-close / entry-close - 1) * 100, in percent.

    Uses the candle whose open-time is at-or-before the target ts.
    Returns None if no eligible entry bar exists.
    Raises ValueError if entry price is zero.
    """
    if not candles:
        return None
    entry_bar = _bar_at_or_before(candles, entry_ts_ms)
    exit_bar = _bar_at_or_before(candles, exit_ts_ms)
    if entry_bar is None or exit_bar is None:
        return None
    p0 = float(entry_bar["c"])
    p1 = float(exit_bar["c"])
    if p0 == 0.0:
        raise ValueError("entry price is zero")
    return (p1 / p0 - 1.0) * 100.0


def _bar_at_or_before(candles: list[dict], ts_ms: int) -> dict | None:
    """Binary search: return the candle whose open-ts is the largest <= ts_ms."""
    if not candles or candles[0]["t"] > ts_ms:
        return None
    lo, hi = 0, len(candles) - 1
    if candles[hi]["t"] <= ts_ms:
        return candles[hi]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if candles[mid]["t"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return candles[lo]


def _fade_pnl(side: str, raw_return_pct: float) -> float:
    """Return the fade-side P&L in percent for a given cascade side.

    Convention (matches build_cascades.py: "A" = ask/sell = liquidation-
    driven buy flow -> price moves UP -> fade = SHORT -> fade_pnl = -raw).
    "B" = bid/buy = liquidation-driven sell flow -> price moves DOWN ->
    fade = LONG -> fade_pnl = +raw.
    """
    if side == "A":
        return -raw_return_pct
    if side == "B":
        return +raw_return_pct
    raise ValueError(f"invalid side: {side!r}")


def _beta(ref_returns: list[float], alt_returns: list[float]) -> float | None:
    """Rolling beta: cov(alt, ref) / var(ref) using sample variance.

    Returns 0.0 if var(ref) is zero (sentinel: no signal), None if
    input has fewer than 2 points, raises if lengths mismatch.
    """
    n = len(ref_returns)
    if n < 2:
        return None
    if n != len(alt_returns):
        raise ValueError("ref and alt return lists must have equal length")
    mean_r = sum(ref_returns) / n
    mean_a = sum(alt_returns) / n
    cov = sum((alt_returns[i] - mean_a) * (ref_returns[i] - mean_r) for i in range(n))
    cov /= n  # population covariance; matches numpy default
    var_r = sum((ref_returns[i] - mean_r) ** 2 for i in range(n)) / n
    if var_r == 0.0:
        return 0.0
    return cov / var_r


def _realized_vol(returns: list[float]) -> float | None:
    """Stddev of 1m returns. Returns None if fewer than 2 points."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    return math.sqrt(var)


def _deviation_from_beta(alt_actual: float, beta: float, ref_actual: float) -> float:
    """alt_actual - (beta * ref_actual), all in percent."""
    return alt_actual - beta * ref_actual


def _funding_bucket(funding: float | None) -> str:
    if funding is None:
        return "unknown"
    if funding > FUNDING_NEUTRAL_BAND:
        return "positive"
    if funding < -FUNDING_NEUTRAL_BAND:
        return "negative"
    return "neutral"


def _oi_direction_bucket(oi_now: float | None, oi_then: float | None) -> str:
    if oi_now is None or oi_then is None or oi_then == 0.0:
        return "unknown"
    delta_pct = (oi_now - oi_then) / oi_then
    if delta_pct > OI_DIRECTION_THRESHOLD_PCT:
        return "up"
    if delta_pct < -OI_DIRECTION_THRESHOLD_PCT:
        return "down"
    return "flat"


def _top_win_share(fade_pnls: list[float]) -> float:
    """Largest single winning trade / total gross profit. Returns 0 if no wins."""
    wins = [p for p in fade_pnls if p > 0]
    if not wins:
        return 0.0
    return max(wins) / sum(wins)


def _confirm_threshold_pct() -> float:
    return CONFIRM_MIN_RETURN_PCT


def _confirms(ref_return_pct: float, alt_return_pct: float, threshold_pct: float) -> bool:
    """True iff ref and alt moved more than threshold in the same direction."""
    if ref_return_pct > threshold_pct and alt_return_pct > threshold_pct:
        return True
    if ref_return_pct < -threshold_pct and alt_return_pct < -threshold_pct:
        return True
    return False


# ---------------------------- candle loaders ------------------------------ #


def _load_candles(symbol: str) -> list[dict]:
    """Load final 1m candle records for symbol across all collected dates.

    The websocket candle stream emits many updates for the same candle.
    Backtests need one row per minute, so keep the last update for each
    candle open timestamp.
    """
    canonical = _canonical_symbol(symbol)
    paths = sorted(CANDLE_DIR.glob(f"{_file_stem(canonical)}_*.jsonl"))
    if not paths:
        return []
    by_open: dict[int, dict] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if not isinstance(payload, dict):
                continue
            if _canonical_symbol(payload.get("s", "")) != canonical:
                continue
            t = payload.get("t")
            c = payload.get("c")
            if t is None or c is None:
                continue
            try:
                by_open[int(t)] = {
                    "t": int(t),
                    "c": float(c),
                    "o": float(payload.get("o", 0)),
                    "h": float(payload.get("h", 0)),
                    "l": float(payload.get("l", 0)),
                    "v": float(payload.get("v", 0)),
                    "n": int(payload.get("n", 0)),
                }
            except (TypeError, ValueError):
                continue
    return [by_open[t] for t in sorted(by_open)]


def _returns_from_candles(candles: list[dict], start_ms: int, end_ms: int) -> list[float]:
    """Fractional 1m returns in the window [start_ms, end_ms]."""
    out: list[float] = []
    prev: float | None = None
    for bar in candles:
        t = bar["t"]
        if t < start_ms or t > end_ms:
            continue
        c = bar["c"]
        if prev is not None and prev != 0.0:
            out.append(c / prev - 1.0)
        prev = c
    return out


# --------------------------- asset_ctx loaders ---------------------------- #


def _load_asset_ctx_series(symbol: str) -> list[dict]:
    """Load all asset_ctx records for a symbol, sorted by poll_ts_ms."""
    canonical = _canonical_symbol(symbol)
    paths = sorted(ASSET_CTX_DIR.glob(f"{_file_stem(canonical)}_*.jsonl"))
    if not paths:
        return []
    out: list[dict] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            poll_ts = rec.get("poll_ts")
            if not poll_ts:
                continue
            try:
                dt = datetime.fromisoformat(poll_ts)
                ts_ms = int(dt.timestamp() * 1000)
            except ValueError:
                continue
            out.append({"ts_ms": ts_ms, "poll_ts": poll_ts, "raw": rec})
    out.sort(key=lambda r: r["ts_ms"])
    return out


def _asset_ctx_at(series: list[dict], ts_ms: int) -> dict | None:
    """Return the asset_ctx record with the largest ts_ms <= target."""
    if not series:
        return None
    lo, hi = 0, len(series) - 1
    if series[hi]["ts_ms"] <= ts_ms:
        return series[hi]
    if series[lo]["ts_ms"] > ts_ms:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if series[mid]["ts_ms"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return series[lo]


def _funding_rate(series: list[dict], ts_ms: int) -> float | None:
    rec = _asset_ctx_at(series, ts_ms)
    if rec is None:
        return None
    raw = rec["raw"]
    # Prefer predicted HlPerp (next-hour funding), else spot HlPerp
    pred = raw.get("predicted", {}).get("HlPerp", {}) or {}
    pred_rate = pred.get("fundingRate")
    if pred_rate is not None:
        try:
            return float(pred_rate)
        except (TypeError, ValueError):
            pass
    ctx = raw.get("context", {}) or {}
    rate = ctx.get("funding")
    if rate is None:
        return None
    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


def _oi_value(series: list[dict], ts_ms: int) -> float | None:
    rec = _asset_ctx_at(series, ts_ms)
    if rec is None:
        return None
    oi = rec["raw"].get("context", {}).get("openInterest")
    if oi is None:
        return None
    try:
        return float(oi)
    except (TypeError, ValueError):
        return None


# --------------------------- cascades loader ------------------------------ #


def _load_cascades() -> list[dict]:
    if not CASCADES_PATH.exists():
        return []
    out: list[dict] = []
    for line in CASCADES_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------- per-event feature computation -------------------- #


def compute_per_event_records(
    cascade: dict,
    candles_by_symbol: dict[str, list[dict]],
    asset_ctx_by_symbol: dict[str, list[dict]] | None = None,
    all_cascades: list[dict] | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict | None:
    """Compute the per-event feature record used by the playbook filters.

    Returns None if the event cannot be evaluated (missing candles, no
    eligible entry bar, or insufficient forward coverage for the longest
    horizon).
    """
    asset_ctx_by_symbol = asset_ctx_by_symbol or {}
    sym = cascade.get("symbol", "")
    side = cascade.get("side", "")
    event_ts_ms = cascade.get("event_ts_ms")
    if event_ts_ms is None or side not in ("A", "B"):
        return None

    alt_candles = candles_by_symbol.get(sym, [])
    btc_candles = candles_by_symbol.get("BTC", [])
    eth_candles = candles_by_symbol.get("ETH", [])
    if not alt_candles or not btc_candles or not eth_candles:
        return None

    max_horizon = max(horizons)
    # Entry bar: first 1m candle whose open is >= event_ts_ms (or use
    # event_ts as a synthetic ts; we then look for bar at or before
    # event_ts_ms + small lag). The first completed bar after the
    # event is the "next minute" bar. We use event_ts + 60_000 as the
    # "look here" anchor (the first full minute bar after the event).
    entry_anchor = event_ts_ms + 60_000
    entry_bar = _bar_at_or_before(alt_candles, entry_anchor)
    if entry_bar is None:
        return None
    if entry_bar["t"] + max_horizon * 60_000 > alt_candles[-1]["t"]:
        # Not enough forward coverage for the longest horizon
        return None

    rec: dict[str, Any] = {
        "symbol": sym,
        "side": side,
        "event_ts": cascade.get("event_ts"),
        "event_ts_ms": event_ts_ms,
        "entry_ts_ms": entry_bar["t"],
        "entry_price": entry_bar["c"],
    }

    # Forward returns: raw (signed by cascade direction) AND fade-signed.
    raw_rets: dict[int, float] = {}
    fade_rets: dict[int, float] = {}
    btc_rets: dict[int, float] = {}
    eth_rets: dict[int, float] = {}
    for h in horizons:
        exit_ts = entry_bar["t"] + h * 60_000
        r_alt = _forward_return(alt_candles, entry_bar["t"], exit_ts)
        r_btc = _forward_return(btc_candles, entry_bar["t"], exit_ts)
        r_eth = _forward_return(eth_candles, entry_bar["t"], exit_ts)
        if r_alt is None or r_btc is None or r_eth is None:
            return None
        raw_rets[h] = r_alt
        fade_rets[h] = _fade_pnl(side, r_alt)
        btc_rets[h] = r_btc
        eth_rets[h] = r_eth
        rec[f"raw_return_{h}m"] = r_alt
        rec[f"fade_pnl_{h}m"] = fade_rets[h]
        rec[f"btc_return_{h}m"] = r_btc
        rec[f"eth_return_{h}m"] = r_eth

    # Rolling beta (alt vs BTC, alt vs ETH) over BETA_WINDOW_MIN before entry.
    beta_start_ms = entry_bar["t"] - BETA_WINDOW_MIN * 60_000
    alt_1m = _returns_from_candles(alt_candles, beta_start_ms, entry_bar["t"])
    btc_1m = _returns_from_candles(btc_candles, beta_start_ms, entry_bar["t"])
    eth_1m = _returns_from_candles(eth_candles, beta_start_ms, entry_bar["t"])
    # Align by index: take min length
    n_min = min(len(alt_1m), len(btc_1m), len(eth_1m))
    if n_min >= 2:
        a = alt_1m[-n_min:]
        b = btc_1m[-n_min:]
        e = eth_1m[-n_min:]
        rec["beta_alt_btc"] = _beta(b, a) or 0.0
        rec["beta_alt_eth"] = _beta(e, a) or 0.0
        rec["beta_window_n"] = n_min
    else:
        rec["beta_alt_btc"] = 0.0
        rec["beta_alt_eth"] = 0.0
        rec["beta_window_n"] = n_min

    # Deviation from expected beta move, at each horizon.
    for h in horizons:
        rec[f"deviation_btc_{h}m"] = _deviation_from_beta(
            raw_rets[h], rec["beta_alt_btc"], btc_rets[h]
        )
        rec[f"deviation_eth_{h}m"] = _deviation_from_beta(
            raw_rets[h], rec["beta_alt_eth"], eth_rets[h]
        )

    # Realized vol (30m pre-event) for BTC and ETH.
    vol_start_ms = entry_bar["t"] - VOL_WINDOW_MIN * 60_000
    btc_vol_30m = _realized_vol(_returns_from_candles(btc_candles, vol_start_ms, entry_bar["t"]))
    eth_vol_30m = _realized_vol(_returns_from_candles(eth_candles, vol_start_ms, entry_bar["t"]))
    rec["btc_vol_30m"] = btc_vol_30m if btc_vol_30m is not None else 0.0
    rec["eth_vol_30m"] = eth_vol_30m if eth_vol_30m is not None else 0.0

    # Per-event confirm flags (depend on fixed threshold, so we can compute
    # these here; the "calm" flags depend on the run median and are added
    # later by _attach_regime_flags).
    threshold_pct = _confirm_threshold_pct()
    for h in horizons:
        alt_r = rec.get(f"raw_return_{h}m", 0.0)
        btc_r = rec.get(f"btc_return_{h}m", 0.0)
        eth_r = rec.get(f"eth_return_{h}m", 0.0)
        rec[f"btc_confirms_alt_{h}m"] = _confirms(btc_r, alt_r, threshold_pct)
        rec[f"eth_confirms_alt_{h}m"] = _confirms(eth_r, alt_r, threshold_pct)
        rec[f"confirms_neither_{h}m"] = not (
            rec[f"btc_confirms_alt_{h}m"] or rec[f"eth_confirms_alt_{h}m"]
        )
        rec[f"confirms_either_{h}m"] = (
            rec[f"btc_confirms_alt_{h}m"] or rec[f"eth_confirms_alt_{h}m"]
        )

    # Funding bucket at event (using alt's own funding).
    if sym in asset_ctx_by_symbol:
        funding = _funding_rate(asset_ctx_by_symbol[sym], event_ts_ms)
        rec["funding_rate"] = funding
        rec["funding_bucket"] = _funding_bucket(funding)
    else:
        rec["funding_rate"] = None
        rec["funding_bucket"] = "unknown"

    # OI direction bucket (alt OI now vs 30m before).
    if sym in asset_ctx_by_symbol:
        oi_now = _oi_value(asset_ctx_by_symbol[sym], event_ts_ms)
        oi_then = _oi_value(asset_ctx_by_symbol[sym], event_ts_ms - 30 * 60_000)
        rec["oi_now"] = oi_now
        rec["oi_then"] = oi_then
        rec["oi_direction"] = _oi_direction_bucket(oi_now, oi_then)
    else:
        rec["oi_now"] = None
        rec["oi_then"] = None
        rec["oi_direction"] = "unknown"

    return rec


def _calm_vol_threshold(events: list[dict]) -> tuple[float, float]:
    """Compute (btc_med, eth_med) for the calm/trending bucket threshold.

    Uses the median of realized vol across the events. If a sample is too
    small (< 5 events) fall back to the absolute CALM_VOL_THRESHOLD.
    """
    btc = [e["btc_vol_30m"] for e in events if e.get("btc_vol_30m") is not None]
    eth = [e["eth_vol_30m"] for e in events if e.get("eth_vol_30m") is not None]
    btc_med = statistics.median(btc) if len(btc) >= 5 else CALM_VOL_THRESHOLD
    eth_med = statistics.median(eth) if len(eth) >= 5 else CALM_VOL_THRESHOLD
    return btc_med, eth_med


def _attach_regime_flags(
    events: list[dict], btc_med: float, eth_med: float
) -> list[dict]:
    """Mutate each event dict in place, adding the calm flags.

    The per-event confirm flags (btc_confirms_alt, eth_confirms_alt,
    confirms_neither, confirms_either) are set in
    compute_per_event_records because they depend only on the fixed
    threshold. This function only attaches the calm flags, which depend
    on the run-wide median.
    """
    for ev in events:
        ev["btc_calm"] = ev["btc_vol_30m"] <= max(btc_med, CALM_VOL_THRESHOLD)
        ev["eth_calm"] = ev["eth_vol_30m"] <= max(eth_med, CALM_VOL_THRESHOLD)
        ev["calm_both"] = ev["btc_calm"] and ev["eth_calm"]
    return events


def _attach_isolation_flag(
    events: list[dict], all_cascades: list[dict]
) -> list[dict]:
    """Tag each event as 'isolated' if no BTC/ETH cascade in the prior window."""
    # Build per-symbol cascade timestamps
    ref_ts: dict[str, list[int]] = defaultdict(list)
    for c in all_cascades:
        sym = c.get("symbol", "")
        if sym in REF_SYMBOLS:
            ts = c.get("event_ts_ms")
            if ts is not None:
                ref_ts[sym].append(ts)
    for sym in ref_ts:
        ref_ts[sym].sort()
    isolation_window_ms = ISOLATION_WINDOW_MIN * 60_000
    for ev in events:
        ts = ev["event_ts_ms"]
        isolated = True
        for sym in REF_SYMBOLS:
            ts_list = ref_ts.get(sym, [])
            # last ref cascade at or before ts
            lo, hi = 0, len(ts_list) - 1
            last_before = None
            if ts_list and ts_list[hi] <= ts:
                last_before = ts_list[hi]
            elif ts_list:
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if ts_list[mid] <= ts:
                        lo = mid
                    else:
                        hi = mid - 1
                if ts_list[lo] <= ts:
                    last_before = ts_list[lo]
            if last_before is not None and ts - last_before <= isolation_window_ms:
                isolated = False
                break
        ev["isolated_30m"] = isolated
    return events


# ---------------------- per-bucket stats + promotion gate ----------------- #


def _bucket_stats(fade_pnls: list[float]) -> dict[str, float | int]:
    n = len(fade_pnls)
    if n == 0:
        return {
            "n": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0, "median_pnl_pct": 0.0,
            "pf": 0.0, "top_win_share": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        }
    wins = [p for p in fade_pnls if p > 0]
    losses = [p for p in fade_pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_pnl_pct": sum(fade_pnls) / n,
        "median_pnl_pct": statistics.median(fade_pnls),
        "pf": pf,
        "top_win_share": _top_win_share(fade_pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }


def apply_promotion_gate(
    events: list[dict], horizon_minutes: int
) -> PromotionVerdict:
    fade_pnls = [ev[f"fade_pnl_{horizon_minutes}m"] for ev in events]
    stats = _bucket_stats(fade_pnls)
    n = stats["n"]
    pf = stats["pf"]
    median = stats["median_pnl_pct"]
    top_share = stats["top_win_share"]
    if n == 0:
        return PromotionVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes,
            n=0, win_rate=0.0, avg_pnl_pct=0.0, median_pnl_pct=0.0, pf=0.0,
            top_win_share=0.0, passed=False, reason="n=0 (empty bucket)",
        )
    if n < PROMOTION_N:
        return PromotionVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False, reason=f"n={n} < {PROMOTION_N} (sample too small)",
        )
    if pf <= PROMOTION_PF:
        return PromotionVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False, reason=f"PF={pf:.2f} <= {PROMOTION_PF}",
        )
    if median <= 0.0:
        return PromotionVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False, reason=f"median={median:+.4f}% <= 0",
        )
    if top_share > PROMOTION_TOP_WIN_SHARE:
        return PromotionVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False,
            reason=f"top_win_share={top_share:.1%} > {PROMOTION_TOP_WIN_SHARE:.0%}",
        )
    return PromotionVerdict(
        symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
        passed=True, reason="all gates met",
    )


# ----------------------------- main --------------------------------------- #


def _safe_pf(pf: float) -> float:
    if pf == float("inf"):
        return 999.0
    return pf


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Relative-value / dislocation backtest (research only)"
    )
    parser.add_argument(
        "--horizons",
        type=str,
        default="5,15,30,60",
        help="Comma-separated forward-return windows in minutes (default 5,15,30,60)",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(ALT_SYMBOLS),
        help=f"Comma-separated alt symbols to evaluate (default {','.join(ALT_SYMBOLS)})",
    )
    args = parser.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(","))
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())

    print("=" * 78)
    print("RELATIVE-VALUE / DISLOCATION BACKTEST — research only")
    print("=" * 78)
    print(f"  Horizons (min):  {horizons}")
    print(f"  Alt symbols:     {symbols}")
    print(f"  Ref symbols:     {REF_SYMBOLS}")
    print(f"  Beta window:     {BETA_WINDOW_MIN}m")
    print(f"  Vol window:      {VOL_WINDOW_MIN}m")
    print(f"  Isolation window:{ISOLATION_WINDOW_MIN}m")
    print(f"  Funding band:    +/-{FUNDING_NEUTRAL_BAND} (neutral inside)")
    print(f"  OI threshold:    +/-{OI_DIRECTION_THRESHOLD_PCT:.0%}")
    print(f"  Confirm min ret: +/-{CONFIRM_MIN_RETURN_PCT:.2f}%")
    print()

    all_cascades = _load_cascades()
    print(f"Loaded {len(all_cascades)} total cascades from {CASCADES_PATH.name}")

    # Filter to the alt symbols under test
    alt_cascades = [c for c in all_cascades if c.get("symbol") in symbols]
    print(f"Filtered to {len(alt_cascades)} alt cascades across {symbols}")

    # Load candles
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in set(symbols) | set(REF_SYMBOLS):
        candles_by_symbol[sym] = _load_candles(sym)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles")

    # Load asset_ctx for funding/OI
    asset_ctx_by_symbol: dict[str, list[dict]] = {}
    for sym in set(symbols):
        asset_ctx_by_symbol[sym] = _load_asset_ctx_series(sym)
        print(f"  {sym}: {len(asset_ctx_by_symbol[sym])} asset_ctx records")

    # Compute per-event records
    print()
    events: list[dict] = []
    skipped_no_candles = 0
    skipped_no_entry = 0
    for c in alt_cascades:
        rec = compute_per_event_records(
            c, candles_by_symbol, asset_ctx_by_symbol, all_cascades, horizons
        )
        if rec is None:
            if not candles_by_symbol.get(c.get("symbol", "")):
                skipped_no_candles += 1
            else:
                skipped_no_entry += 1
            continue
        events.append(rec)
    print(f"Per-event records computed: {len(events)}")
    print(f"  Skipped (no candles for sym): {skipped_no_candles}")
    print(f"  Skipped (no entry / no exit): {skipped_no_entry}")

    if not events:
        print("No events to evaluate. Exiting.")
        return 1

    btc_med, eth_med = _calm_vol_threshold(events)
    print(f"  BTC vol-30m median: {btc_med:.6f}")
    print(f"  ETH vol-30m median: {eth_med:.6f}")
    events = _attach_regime_flags(events, btc_med, eth_med)
    events = _attach_isolation_flag(events, all_cascades)

    # Build per-symbol, per-horizon, per-playbook verdicts.
    playbooks = (
        ("generic", lambda ev, h: True),
        ("H1_btc_eth_calm", lambda ev, h: ev["btc_calm"] and ev["eth_calm"]),
        ("H2_btc_eth_dont_confirm", lambda ev, h: ev[f"confirms_neither_{h}m"]),
        ("H3_isolated", lambda ev, h: ev["isolated_30m"]),
    )

    results: list[dict] = []
    print()
    print("=" * 78)
    print("RESULTS — per symbol, per horizon, per playbook")
    print("=" * 78)
    header = (
        f"  {'sym':<11} {'horizon':>7} {'playbook':<25} {'n':>4} "
        f"{'WR%':>6} {'avg%':>9} {'med%':>9} {'PF':>7} {'top%':>6}  verdict"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for sym in symbols:
        sym_events = [e for e in events if e["symbol"] == sym]
        if not sym_events:
            continue
        for h in horizons:
            for pb_name, pb_filter in playbooks:
                bucket = [e for e in sym_events if pb_filter(e, h)]
                v = apply_promotion_gate(bucket, h)
                v.symbol = sym
                v.playbook = pb_name
                pf_str = f"{_safe_pf(v.pf):>6.2f}"
                verdict = "PASS" if v.passed else v.reason
                print(
                    f"  {sym:<11} {h:>5}m {pb_name:<25} {v.n:>4} "
                    f"{v.win_rate*100:>5.1f}% {v.avg_pnl_pct:>+8.4f} "
                    f"{v.median_pnl_pct:>+8.4f} {pf_str:>7} "
                    f"{v.top_win_share*100:>5.1f}%  {verdict}"
                )
                results.append(asdict(v))

    # Write JSON output
    RESULTS_JSON_PATH.write_text(
        json.dumps(
            {
                "horizons": list(horizons),
                "symbols": list(symbols),
                "constants": {
                    "BETA_WINDOW_MIN": BETA_WINDOW_MIN,
                    "VOL_WINDOW_MIN": VOL_WINDOW_MIN,
                    "ISOLATION_WINDOW_MIN": ISOLATION_WINDOW_MIN,
                    "FUNDING_NEUTRAL_BAND": FUNDING_NEUTRAL_BAND,
                    "OI_DIRECTION_THRESHOLD_PCT": OI_DIRECTION_THRESHOLD_PCT,
                    "CONFIRM_MIN_RETURN_PCT": CONFIRM_MIN_RETURN_PCT,
                    "PROMOTION_N": PROMOTION_N,
                    "PROMOTION_PF": PROMOTION_PF,
                    "PROMOTION_TOP_WIN_SHARE": PROMOTION_TOP_WIN_SHARE,
                },
                "totals": {
                    "alt_cascades": len(alt_cascades),
                    "events_evaluated": len(events),
                    "skipped_no_candles": skipped_no_candles,
                    "skipped_no_entry": skipped_no_entry,
                    "btc_vol_30m_median": btc_med,
                    "eth_vol_30m_median": eth_med,
                },
                "verdicts": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote JSON: {RESULTS_JSON_PATH.name}")

    # Write Markdown summary
    _write_summary_md(results, events, symbols, horizons, skipped_no_candles,
                      skipped_no_entry, btc_med, eth_med)
    print(f"Wrote MD:   {SUMMARY_MD_PATH.name}")
    print()
    print("DONE. No execution touched. Outputs are research-only.")
    return 0


def _write_summary_md(
    results: list[dict],
    events: list[dict],
    symbols: tuple[str, ...],
    horizons: tuple[int, ...],
    skipped_no_candles: int,
    skipped_no_entry: int,
    btc_med: float,
    eth_med: float,
) -> None:
    lines: list[str] = []
    lines.append("# Relative-Value / Dislocation Backtest — Summary")
    lines.append("")
    lines.append("Research only. No execution wiring. No strategy promotion.")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Per-symbol, per-horizon, per-playbook")
    lines.append("")
    lines.append(
        "| Symbol | Horizon | Playbook | n | WR% | avg% | med% | PF | top_win_share% | Verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda x: (x["symbol"], x["horizon_minutes"], x["playbook"])):
        verdict = "**PASS**" if r["passed"] else r["reason"]
        pf = _safe_pf(r["pf"])
        lines.append(
            f"| {r['symbol']} | {r['horizon_minutes']}m | {r['playbook']} | "
            f"{r['n']} | {r['win_rate']*100:.1f} | {r['avg_pnl_pct']:+.4f} | "
            f"{r['median_pnl_pct']:+.4f} | {pf:.2f} | {r['top_win_share']*100:.1f} | "
            f"{verdict} |"
        )
    lines.append("")
    lines.append("## Pass summary (n >= 30, PF > 1.5, median > 0, top_win_share <= 35%)")
    lines.append("")
    passes = [r for r in results if r["passed"]]
    if not passes:
        lines.append("No symbol/playbook/horizon combination passed all four gates.")
    else:
        lines.append("| Symbol | Playbook | Horizon | n | PF | med% | top_win_share% |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(passes, key=lambda x: (x["symbol"], x["playbook"], x["horizon_minutes"])):
            pf = _safe_pf(r["pf"])
            lines.append(
                f"| {r['symbol']} | {r['playbook']} | {r['horizon_minutes']}m | "
                f"{r['n']} | {pf:.2f} | {r['median_pnl_pct']:+.4f} | "
                f"{r['top_win_share']*100:.1f} |"
            )
    lines.append("")
    lines.append("## Coverage notes")
    lines.append("")
    lines.append(f"- Total alt cascades seen: {sum(1 for e in events)} events (after candle/entry filters)")
    lines.append(f"- Skipped (no candle data for symbol): {skipped_no_candles}")
    lines.append(f"- Skipped (no eligible entry / no exit bar): {skipped_no_entry}")
    lines.append(f"- BTC vol-30m median (calm threshold): {btc_med:.6f}")
    lines.append(f"- ETH vol-30m median (calm threshold): {eth_med:.6f}")
    lines.append("")
    lines.append("## Symbols evaluated")
    lines.append("")
    for sym in symbols:
        n = sum(1 for e in events if e["symbol"] == sym)
        lines.append(f"- `{sym}`: {n} events")
    lines.append("")
    lines.append("## How to read")
    lines.append("")
    lines.append("- `generic` = all events, no filter (control).")
    lines.append("- `H1_btc_eth_calm` = only fade when BTC AND ETH realized vol is below the run median.")
    lines.append("- `H2_btc_eth_dont_confirm` = only fade when neither ref moved in the same direction as the alt cascade beyond the confirm threshold.")
    lines.append("- `H3_isolated` = only fade when no BTC/ETH cascade happened in the prior 30m (i.e. the alt move was alt-specific, not dragged by majors).")
    lines.append("- Returns are in **percent units** (e.g. `+0.32` means `+0.32%`).")
    lines.append("- PF is dimensionless. `top_win_share` is the largest single win as a fraction of total gross profit.")
    lines.append("")
    SUMMARY_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

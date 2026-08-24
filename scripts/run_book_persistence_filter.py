"""Book-persistence / stale-book / trade-flow filter backtest (research only).

For each BTC/ETH liquidation cascade event, compute book-side features
that prior research flagged as candidates for adding edge to the
decaying simple-fade rule:

  1. BBO/L2 imbalance at event time and how long that imbalance has been
     persistent (consecutive same-direction snapshots before the event).
  2. Stale-book flag: spread has widened AND mid has not moved (low
     activity regime before the cascade).
  3. Post-event trade-flow imbalance at 30s and 60s (who's hitting the
     book in the immediate aftermath? Amplifies or fades the cascade?).
  4. Post-event book drift at 30s/60s (does the book absorb or amplify?).

Then test playbook filters per cascade against the standard promotion
gate, per-symbol.

HARD SCOPE: research only. Does NOT touch execution, order_manager,
risk.py, or any live/paper routing. BTC/ETH only (v1 symbols).

Run:
    python scripts/run_book_persistence_filter.py
    python scripts/run_book_persistence_filter.py --horizons 5,15,30,60
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.data_files import open_data_file, data_file_exists, iter_data_files

from src.strategy.event_features import _canonical_symbol, _file_stem  # noqa: E402


# ----------------------------- constants ---------------------------------- #

# Per Slim's 2026-08-06 spec: BTC/ETH only.
SYMBOLS: tuple[str, ...] = ("BTC", "ETH")

# Forward-return windows in minutes.
DEFAULT_HORIZONS: tuple[int, ...] = (5, 15, 30, 60)

# BBO/L2 imbalance buckets.
#   imbalance > BBO_HEAVY_THRESHOLD       -> bid_heavy
#   imbalance < 1 - BBO_HEAVY_THRESHOLD   -> ask_heavy
#   else                                  -> balanced
BBO_HEAVY_THRESHOLD: float = 0.55  # > 0.55 is bid_heavy; < 0.45 is ask_heavy

# Trade-flow neutrality band. |flow_imbalance| < this is "neutral".
FLOW_NEUTRAL_BAND: float = 0.20

# Persistence: at least this many seconds of consecutive same-direction
# BBO snapshots for the book to be flagged "persistent_X".
PERSISTENCE_MIN_SECONDS: float = 30.0

# L2 top-N levels for the depth-weighted imbalance.
L2_LEVELS: int = 5

# Stale-book detection.
SPREAD_WIDEN_FACTOR: float = 1.5  # current_spread > 1.5x median spread = widened
STALE_MID_DRIFT_PCT: float = 0.01  # abs(mid_drift / mid_prior) < 0.01% = mid unchanged

# Lookback for spread-median computation.
STALE_LOOKBACK_MS: int = 5 * 60_000  # 5 minutes

# Promotion gate (same as the rest of the backtest rig).
PROMOTION_N: int = 30
PROMOTION_PF: float = 1.5
PROMOTION_TOP_WIN_SHARE: float = 0.35

# Paths.
CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
BBO_DIR = REPO_ROOT / "data" / "ws_bbo"
L2_DIR = REPO_ROOT / "data" / "ws_l2book"
TRADES_DIR = REPO_ROOT / "data" / "ws_trades"
RESULTS_JSON_PATH = REPO_ROOT / "data" / "book_persistence_filter_results.json"
SUMMARY_MD_PATH = REPO_ROOT / "data" / "book_persistence_filter_summary.md"


# ----------------------------- dataclasses ------------------------------- #


@dataclass
class BucketVerdict:
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


# --------------------------- parsers -------------------------------------- #


def _parse_bbo(row: dict) -> tuple[float, float, float, float, int] | None:
    """Parse a ws_bbo row -> (bid_px, bid_sz, ask_px, ask_sz, ts_ms)."""
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        return None
    bbo = payload.get("bbo")
    if not isinstance(bbo, list) or len(bbo) < 2:
        return None
    bid = bbo[0]
    ask = bbo[1]
    if not (isinstance(bid, dict) and isinstance(ask, dict)):
        return None
    try:
        bid_px = float(bid.get("px", 0) or 0)
        bid_sz = float(bid.get("sz", 0) or 0)
        ask_px = float(ask.get("px", 0) or 0)
        ask_sz = float(ask.get("sz", 0) or 0)
        ts = int(payload.get("time") or payload.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    return bid_px, bid_sz, ask_px, ask_sz, ts


def _parse_l2(row: dict) -> tuple[list, list, float, float, int] | None:
    """Parse a ws_l2book row -> (bids, asks, mid, spread, ts_ms)."""
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        return None
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return None
    bids_raw = levels[0]
    asks_raw = levels[1]
    if not (isinstance(bids_raw, list) and isinstance(asks_raw, list)):
        return None
    bids = []
    for lvl in bids_raw:
        if not isinstance(lvl, dict):
            continue
        try:
            bids.append((float(lvl.get("px", 0) or 0), float(lvl.get("sz", 0) or 0)))
        except (TypeError, ValueError):
            continue
    asks = []
    for lvl in asks_raw:
        if not isinstance(lvl, dict):
            continue
        try:
            asks.append((float(lvl.get("px", 0) or 0), float(lvl.get("sz", 0) or 0)))
        except (TypeError, ValueError):
            continue
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2.0
    try:
        spread = float(payload.get("spread", 0) or 0)
    except (TypeError, ValueError):
        spread = asks[0][0] - bids[0][0]
    try:
        ts = int(payload.get("time") or payload.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    return bids, asks, mid, spread, ts


def _parse_trade(row: dict) -> tuple[str, float, float, int] | None:
    """Parse a ws_trades row -> (side, px, sz, time_ms)."""
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        return None
    side = payload.get("side")
    if side not in ("B", "A"):
        return None
    try:
        px = float(payload.get("px", 0) or 0)
        sz = float(payload.get("sz", 0) or 0)
        ts = int(payload.get("time") or payload.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if px == 0.0 or sz == 0.0:
        return None
    return side, px, sz, ts


# --------------------------- data loaders --------------------------------- #


def _bar_at_or_before(bars: list[dict], ts_ms: int) -> dict | None:
    """Binary search: return the bar whose ts is the largest <= ts_ms.

    Accepts both 't' (candles) and 'ts' (BBO/L2) keys for the timestamp.
    """
    if not bars:
        return None
    # Pick whichever key the caller uses
    sample = bars[0]
    if "t" in sample:
        key = "t"
    elif "ts" in sample:
        key = "ts"
    else:
        return None
    if sample[key] > ts_ms:
        return None
    lo, hi = 0, len(bars) - 1
    if bars[hi][key] <= ts_ms:
        return bars[hi]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid][key] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return bars[lo]


def _load_bbo(symbol: str) -> list[dict]:
    """Load BBO snapshots for symbol, sorted by ts_ms."""
    canonical = _canonical_symbol(symbol)
    paths = iter_data_files(BBO_DIR, f"{_file_stem(canonical)}_*.jsonl")
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
            parsed = _parse_bbo(rec)
            if parsed is None:
                continue
            bid_px, bid_sz, ask_px, ask_sz, ts = parsed
            out.append(
                {
                    "ts": ts,
                    "bid_px": bid_px,
                    "bid_sz": bid_sz,
                    "ask_px": ask_px,
                    "ask_sz": ask_sz,
                }
            )
    out.sort(key=lambda r: r["ts"])
    return out


def _load_l2(symbol: str) -> list[dict]:
    """Load L2 snapshots for symbol, sorted by ts_ms."""
    canonical = _canonical_symbol(symbol)
    paths = iter_data_files(L2_DIR, f"{_file_stem(canonical)}_*.jsonl")
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
            parsed = _parse_l2(rec)
            if parsed is None:
                continue
            bids, asks, mid, spread, ts = parsed
            out.append(
                {
                    "ts": ts,
                    "bids": bids,
                    "asks": asks,
                    "mid": mid,
                    "spread": spread,
                }
            )
    out.sort(key=lambda r: r["ts"])
    return out


def _load_trades(symbol: str) -> list[dict]:
    """Load raw trades for symbol, sorted by time_ms."""
    canonical = _canonical_symbol(symbol)
    paths = iter_data_files(TRADES_DIR, f"{_file_stem(canonical)}_*.jsonl")
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
            parsed = _parse_trade(rec)
            if parsed is None:
                continue
            side, px, sz, ts = parsed
            out.append({"ts": ts, "side": side, "px": px, "sz": sz})
    out.sort(key=lambda r: r["ts"])
    return out


def _load_candles(symbol: str) -> list[dict]:
    """Load 1m candles for symbol, dedup to last update per bar, sorted by t."""
    canonical = _canonical_symbol(symbol)
    paths = iter_data_files(REPO_ROOT / "data" / "ws_candle", f"{_file_stem(canonical)}_*.jsonl")
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


# --------------------------- math ----------------------------------------- #


def _bbo_imbalance(bid_sz: float, ask_sz: float) -> float:
    """bid / (bid + ask). Both zero -> 0.5 (sentinel: no signal)."""
    total = bid_sz + ask_sz
    if total == 0.0:
        return 0.5
    return bid_sz / total


def _l2_imbalance(bids: list, asks: list, n: int) -> float:
    """Top-N depth-weighted imbalance.

    Returns:
      1.0 if asks are empty (all bids, extreme bid_heavy)
      0.0 if bids are empty (all asks, extreme ask_heavy)
      0.5 if both empty (sentinel: no signal)
    """
    if not bids and not asks:
        return 0.5
    if not bids:
        return 0.0
    if not asks:
        return 1.0
    bid_total = sum(sz for _, sz in bids[:n])
    ask_total = sum(sz for _, sz in asks[:n])
    if bid_total == 0.0 and ask_total == 0.0:
        return 0.5
    return bid_total / (bid_total + ask_total)


def _bbo_imbalance_bucket(imbalance: float) -> str:
    if imbalance > BBO_HEAVY_THRESHOLD:
        return "bid_heavy"
    if imbalance < 1.0 - BBO_HEAVY_THRESHOLD:
        return "ask_heavy"
    return "balanced"


def _l2_imbalance_bucket(imbalance: float) -> str:
    return _bbo_imbalance_bucket(imbalance)


def _mid_from_bbo(bid_px: float, ask_px: float) -> float:
    return (bid_px + ask_px) / 2.0


def _mid_from_l2(bids: list, asks: list) -> float:
    if not bids or not asks:
        return 0.0
    return (bids[0][0] + asks[0][0]) / 2.0


def _persistence_seconds(
    snaps: list[tuple[int, float]], event_ts_ms: int
) -> dict[str, Any]:
    """Count consecutive same-direction imbalance snapshots before event_ts_ms.

    Each snap is (ts_ms, imbalance). Returns {duration_seconds, direction}.
    Duration is the wall-clock span of the current streak ending at or
    just before event_ts_ms.
    """
    if not snaps:
        return {"duration_seconds": 0.0, "direction": "neutral"}
    # Filter to <= event_ts_ms, sort by ts ascending
    relevant = sorted(
        [(t, imb) for t, imb in snaps if t <= event_ts_ms], key=lambda x: x[0]
    )
    if not relevant:
        return {"duration_seconds": 0.0, "direction": "neutral"}
    # Walk backwards from the most recent snap, counting consecutive same-direction
    last_dir = _bbo_imbalance_bucket(relevant[-1][1])
    if last_dir == "balanced":
        return {"duration_seconds": 0.0, "direction": "neutral"}
    first_ts_in_streak = relevant[-1][0]
    for t, imb in reversed(relevant[:-1]):
        d = _bbo_imbalance_bucket(imb)
        if d == last_dir:
            first_ts_in_streak = t
        else:
            break
    duration_seconds = (relevant[-1][0] - first_ts_in_streak) / 1000.0 + 1.0
    return {"duration_seconds": duration_seconds, "direction": last_dir}


def _stale_book_flag(
    current_spread: float, median_spread: float, current_mid: float, prior_mid: float
) -> dict[str, Any]:
    """Determine if the book is 'stale' at the event time.

    stale_book = spread_widened AND mid_unchanged
    """
    spread_widened = (
        median_spread > 0
        and current_spread > SPREAD_WIDEN_FACTOR * median_spread
    )
    if prior_mid > 0 and current_mid > 0:
        drift_pct = abs(current_mid - prior_mid) / prior_mid * 100.0
        mid_unchanged = drift_pct < STALE_MID_DRIFT_PCT
    else:
        mid_unchanged = False
    return {
        "spread_widened": spread_widened,
        "mid_unchanged": mid_unchanged,
        "stale_book": spread_widened and mid_unchanged,
    }


def _trades_in_window(
    trades: list[dict], start_ms: int, end_ms: int
) -> list[dict]:
    """Slice of trades with ts in [start_ms, end_ms] (assumes trades sorted by ts)."""
    if not trades:
        return []
    # Binary search for left bound (first ts >= start_ms)
    lo, hi = 0, len(trades)
    while lo < hi:
        mid = (lo + hi) // 2
        if trades[mid]["ts"] < start_ms:
            lo = mid + 1
        else:
            hi = mid
    left = lo
    # Right bound (first ts > end_ms)
    lo, hi = left, len(trades)
    while lo < hi:
        mid = (lo + hi) // 2
        if trades[mid]["ts"] <= end_ms:
            lo = mid + 1
        else:
            hi = mid
    right = lo
    return trades[left:right]


def _bbo_imbalance_series(
    bbo_snaps: list[dict], start_ms: int, end_ms: int
) -> list[tuple[int, float]]:
    """Return [(ts_ms, bbo_imbalance)] for bbo_snaps in [start_ms, end_ms].

    Uses binary search on the time-sorted bbo_snaps to avoid scanning
    the full list (which can be 2M+ entries).
    """
    if not bbo_snaps:
        return []
    # Binary search for left
    lo, hi = 0, len(bbo_snaps)
    while lo < hi:
        mid = (lo + hi) // 2
        if bbo_snaps[mid]["ts"] < start_ms:
            lo = mid + 1
        else:
            hi = mid
    left = lo
    lo, hi = left, len(bbo_snaps)
    while lo < hi:
        mid = (lo + hi) // 2
        if bbo_snaps[mid]["ts"] <= end_ms:
            lo = mid + 1
        else:
            hi = mid
    right = lo
    out: list[tuple[int, float]] = []
    for s in bbo_snaps[left:right]:
        out.append((s["ts"], _bbo_imbalance(s["bid_sz"], s["ask_sz"])))
    return out


def _bbo_window(
    bbo_snaps: list[dict], start_ms: int, end_ms: int
) -> list[dict]:
    """Return bbo_snaps in [start_ms, end_ms] (binary-search window)."""
    if not bbo_snaps:
        return []
    lo, hi = 0, len(bbo_snaps)
    while lo < hi:
        mid = (lo + hi) // 2
        if bbo_snaps[mid]["ts"] < start_ms:
            lo = mid + 1
        else:
            hi = mid
    left = lo
    lo, hi = left, len(bbo_snaps)
    while lo < hi:
        mid = (lo + hi) // 2
        if bbo_snaps[mid]["ts"] <= end_ms:
            lo = mid + 1
        else:
            hi = mid
    right = lo
    return bbo_snaps[left:right]


def _flow_stats(trades: Iterable[tuple[str, float, float]]) -> dict[str, Any]:
    """Compute trade-flow imbalance over a window of (side, px, sz) tuples.

    flow_imbalance = (buy_count - sell_count) / total_count
    notional_imbalance = (buy_notional - sell_notional) / total_notional
    """
    buys = 0
    sells = 0
    buy_notional = 0.0
    sell_notional = 0.0
    for side, px, sz in trades:
        if side == "B":
            buys += 1
            buy_notional += px * sz
        elif side == "A":
            sells += 1
            sell_notional += px * sz
    total = buys + sells
    notional_total = buy_notional + sell_notional
    return {
        "buy_count": buys,
        "sell_count": sells,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "flow_imbalance": (buys - sells) / total if total else 0.0,
        "notional_imbalance": (
            (buy_notional - sell_notional) / notional_total
            if notional_total > 0
            else 0.0
        ),
    }


def _flow_amplifies_flag(side: str, flow_imbalance: float, window_s: int) -> str:
    """Categorize post-event trade flow relative to the cascade direction.

    side A = forced buy flow (cascade up). Amplifies = positive flow.
    side B = forced sell flow (cascade down). Amplifies = negative flow.
    """
    if abs(flow_imbalance) < FLOW_NEUTRAL_BAND:
        return "neutral"
    if side == "A":
        return "amplifies" if flow_imbalance > 0 else "fades"
    if side == "B":
        return "amplifies" if flow_imbalance < 0 else "fades"
    return "neutral"


def _book_absorbed_flag(
    side: str, bbo_imbalance_at_event: float, bbo_imbalance_post: float
) -> str:
    """Categorize post-event book drift relative to cascade direction.

    side A (cascade up) absorbed = book became ask_heavy (sells rebuilt)
    side B (cascade down) absorbed = book became bid_heavy (buys rebuilt)
    """
    delta = bbo_imbalance_post - bbo_imbalance_at_event
    # Use strict < to avoid float-precision edge cases at the band boundary.
    if abs(delta) < FLOW_NEUTRAL_BAND:
        return "neutral"
    if side == "A":
        return "absorbed" if delta < 0 else "amplified"
    if side == "B":
        return "absorbed" if delta > 0 else "amplified"
    return "neutral"


# ------------------------- per-event feature assembly -------------------- #


def compute_per_event_features(
    cascade: dict,
    bbo_snaps: list[dict],
    l2_snaps: list[dict],
    trades_30s: list[tuple[str, float, float]],
    trades_60s: list[tuple[str, float, float]],
    candles: list[dict] | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict | None:
    """Compute the per-event feature record.

    Returns None if the event cannot be evaluated.
    """
    sym = cascade.get("symbol", "")
    side = cascade.get("side", "")
    event_ts_ms = cascade.get("event_ts_ms")
    if event_ts_ms is None or side not in ("A", "B"):
        return None

    # BBO at event (latest snapshot at or before event_ts_ms)
    bbo_at_event = _bar_at_or_before(bbo_snaps, event_ts_ms)
    if bbo_at_event is None:
        return None

    bbo_imb = _bbo_imbalance(bbo_at_event["bid_sz"], bbo_at_event["ask_sz"])
    bbo_mid = _mid_from_bbo(bbo_at_event["bid_px"], bbo_at_event["ask_px"])

    # L2 at event (latest snapshot at or before event_ts_ms)
    l2_at_event = _bar_at_or_before(l2_snaps, event_ts_ms)
    if l2_at_event is not None:
        l2_imb = _l2_imbalance(l2_at_event["bids"], l2_at_event["asks"], L2_LEVELS)
        l2_spread = l2_at_event["spread"]
    else:
        l2_imb = 0.5
        l2_spread = bbo_at_event["ask_px"] - bbo_at_event["bid_px"]

    # BBO persistence (snapshots in [event_ts - 5min, event_ts]) via binary window
    persist_window_start = event_ts_ms - 5 * 60_000
    persist_snaps = _bbo_imbalance_series(bbo_snaps, persist_window_start, event_ts_ms)
    persist = _persistence_seconds(persist_snaps, event_ts_ms)

    # Stale book: spread now vs median over prior 5 min, mid now vs 5 min ago
    lookback_snaps = _bbo_window(bbo_snaps, event_ts_ms - STALE_LOOKBACK_MS, event_ts_ms - 1)
    if lookback_snaps:
        median_spread = statistics.median(
            s["ask_px"] - s["bid_px"] for s in lookback_snaps
        )
        prior_mid = _mid_from_bbo(lookback_snaps[0]["bid_px"], lookback_snaps[0]["ask_px"])
    else:
        median_spread = l2_spread
        prior_mid = bbo_mid
    stale = _stale_book_flag(
        current_spread=l2_spread,
        median_spread=median_spread,
        current_mid=bbo_mid,
        prior_mid=prior_mid,
    )

    # Post-event book drift: BBO imbalance at 30s/60s post-event
    bbo_30s = _bar_at_or_before(bbo_snaps, event_ts_ms + 30_000)
    bbo_60s = _bar_at_or_before(bbo_snaps, event_ts_ms + 60_000)
    if bbo_30s is not None:
        bbo_imb_30s = _bbo_imbalance(bbo_30s["bid_sz"], bbo_30s["ask_sz"])
    else:
        bbo_imb_30s = bbo_imb
    if bbo_60s is not None:
        bbo_imb_60s = _bbo_imbalance(bbo_60s["bid_sz"], bbo_60s["ask_sz"])
    else:
        bbo_imb_60s = bbo_imb

    book_30s = _book_absorbed_flag(side, bbo_imb, bbo_imb_30s)
    book_60s = _book_absorbed_flag(side, bbo_imb, bbo_imb_60s)

    # Trade-flow imbalance at 30s and 60s
    flow_30s = _flow_stats(trades_30s)
    flow_60s = _flow_stats(trades_60s)
    flow_30s_label = _flow_amplifies_flag(side, flow_30s["flow_imbalance"], 30)
    flow_60s_label = _flow_amplifies_flag(side, flow_60s["flow_imbalance"], 60)

    # Forward returns (uses 1m candles)
    fade_rets: dict[int, float] = {}
    if candles is not None:
        entry_anchor = event_ts_ms + 60_000
        entry_bar = _bar_at_or_before(candles, entry_anchor)
        if entry_bar is None:
            return None
        max_horizon = max(horizons)
        if entry_bar["t"] + max_horizon * 60_000 > candles[-1]["t"]:
            return None
        from scripts.run_relative_value_dislocation import (  # noqa: PLC0415
            _fade_pnl as _rv_fade_pnl,
            _forward_return as _rv_forward_return,
        )
        for h in horizons:
            exit_ts = entry_bar["t"] + h * 60_000
            r = _rv_forward_return(candles, entry_bar["t"], exit_ts)
            if r is None:
                return None
            fade_rets[h] = _rv_fade_pnl(side, r)

    rec: dict[str, Any] = {
        "symbol": sym,
        "side": side,
        "event_ts": cascade.get("event_ts"),
        "event_ts_ms": event_ts_ms,
        "bbo_imbalance_at_event": bbo_imb,
        "bbo_bucket_at_event": _bbo_imbalance_bucket(bbo_imb),
        "l2_imbalance_at_event": l2_imb,
        "l2_bucket_at_event": _l2_imbalance_bucket(l2_imb),
        "bbo_mid_at_event": bbo_mid,
        "l2_spread_at_event": l2_spread,
        "median_spread_5m": median_spread,
        "persistence_seconds": persist["duration_seconds"],
        "persistence_direction": persist["direction"],
        "spread_widened": stale["spread_widened"],
        "mid_unchanged": stale["mid_unchanged"],
        "stale_book": stale["stale_book"],
        "bbo_imbalance_30s": bbo_imb_30s,
        "bbo_imbalance_60s": bbo_imb_60s,
        "book_30s_label": book_30s,
        "book_60s_label": book_60s,
        "flow_imbalance_30s": flow_30s["flow_imbalance"],
        "flow_imbalance_60s": flow_60s["flow_imbalance"],
        "flow_notional_imbalance_30s": flow_30s["notional_imbalance"],
        "flow_notional_imbalance_60s": flow_60s["notional_imbalance"],
        "flow_buy_count_30s": flow_30s["buy_count"],
        "flow_sell_count_30s": flow_30s["sell_count"],
        "flow_buy_count_60s": flow_60s["buy_count"],
        "flow_sell_count_60s": flow_60s["sell_count"],
        "flow_30s_label": flow_30s_label,
        "flow_60s_label": flow_60s_label,
    }
    for h, pnl in fade_rets.items():
        rec[f"fade_pnl_{h}m"] = pnl
    return rec


# ---------------------- per-bucket stats + promotion gate ---------------- #


def _bucket_stats(fade_pnls: list[float]) -> dict[str, Any]:
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
    top_win_share = (max(wins) / gross_profit) if wins else 0.0
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_pnl_pct": sum(fade_pnls) / n,
        "median_pnl_pct": statistics.median(fade_pnls),
        "pf": pf,
        "top_win_share": top_win_share,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }


def apply_promotion_gate(
    events: list[dict], horizon_minutes: int
) -> BucketVerdict:
    fade_pnls = [ev[f"fade_pnl_{horizon_minutes}m"] for ev in events]
    stats = _bucket_stats(fade_pnls)
    n = stats["n"]
    pf = stats["pf"]
    median = stats["median_pnl_pct"]
    top_share = stats["top_win_share"]
    if n == 0:
        return BucketVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes,
            n=0, win_rate=0.0, avg_pnl_pct=0.0, median_pnl_pct=0.0, pf=0.0,
            top_win_share=0.0, passed=False, reason="n=0 (empty bucket)",
        )
    if n < PROMOTION_N:
        return BucketVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False, reason=f"n={n} < {PROMOTION_N} (sample too small)",
        )
    if pf <= PROMOTION_PF:
        return BucketVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False, reason=f"PF={pf:.2f} <= {PROMOTION_PF}",
        )
    if median <= 0.0:
        return BucketVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False, reason=f"median={median:+.4f}% <= 0",
        )
    if top_share > PROMOTION_TOP_WIN_SHARE:
        return BucketVerdict(
            symbol="", playbook="", horizon_minutes=horizon_minutes, **stats,
            passed=False,
            reason=f"top_win_share={top_share:.1%} > {PROMOTION_TOP_WIN_SHARE:.0%}",
        )
    return BucketVerdict(
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
        description="Book-persistence / stale-book / trade-flow filter backtest "
                    "(research only, BTC/ETH)"
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
        default=",".join(SYMBOLS),
        help=f"Comma-separated symbols to evaluate (default {','.join(SYMBOLS)})",
    )
    args = parser.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(","))
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())

    print("=" * 78)
    print("BOOK-PERSISTENCE / STALE-BOOK / TRADE-FLOW FILTER — research only")
    print("=" * 78)
    print(f"  Symbols:           {symbols}")
    print(f"  Horizons (min):    {horizons}")
    print(f"  BBO heavy band:    > {BBO_HEAVY_THRESHOLD} (or < {1.0 - BBO_HEAVY_THRESHOLD})")
    print(f"  Persistence min:   {PERSISTENCE_MIN_SECONDS}s of consecutive same-direction")
    print(f"  L2 top-N:          {L2_LEVELS} levels")
    print(f"  Spread widen:      > {SPREAD_WIDEN_FACTOR}x median(5m)")
    print(f"  Mid unchanged:     drift < {STALE_MID_DRIFT_PCT}% over 5m")
    print(f"  Flow neutral band: +/-{FLOW_NEUTRAL_BAND}")
    print(f"  Promotion gate:    n>={PROMOTION_N} PF>{PROMOTION_PF} med>0 top_win_share<={PROMOTION_TOP_WIN_SHARE:.0%}")
    print()

    # Load data
    all_cascades = _load_cascades()
    print(f"Loaded {len(all_cascades)} total cascades from {CASCADES_PATH.name}")
    target_cascades = [c for c in all_cascades if c.get("symbol") in symbols]
    print(f"Filtered to {len(target_cascades)} cascades across {symbols}")

    bbo_by_symbol: dict[str, list[dict]] = {}
    l2_by_symbol: dict[str, list[dict]] = {}
    trades_by_symbol: dict[str, list[dict]] = {}
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        bbo_by_symbol[sym] = _load_bbo(sym)
        l2_by_symbol[sym] = _load_l2(sym)
        trades_by_symbol[sym] = _load_trades(sym)
        candles_by_symbol[sym] = _load_candles(sym)
        print(
            f"  {sym}: {len(bbo_by_symbol[sym])} BBO | {len(l2_by_symbol[sym])} L2 | "
            f"{len(trades_by_symbol[sym])} trades | {len(candles_by_symbol[sym])} candles"
        )

    # Compute per-event features
    print()
    print("Computing per-event features...")
    events: list[dict] = []
    n_no_bbo = 0
    n_no_l2 = 0
    n_no_candles = 0
    n_no_entry = 0
    for c in target_cascades:
        sym = c.get("symbol", "")
        event_ts_ms = c.get("event_ts_ms")
        if event_ts_ms is None:
            continue
        bbo_snaps = bbo_by_symbol.get(sym, [])
        l2_snaps = l2_by_symbol.get(sym, [])
        all_trades = trades_by_symbol.get(sym, [])
        candles = candles_by_symbol.get(sym, [])

        if not bbo_snaps:
            n_no_bbo += 1
            continue
        if not l2_snaps:
            n_no_l2 += 1
        if not candles:
            n_no_candles += 1
            continue

        # 30s and 60s trade windows via binary-search slice (O(log N + k))
        trades_30s_raw = _trades_in_window(all_trades, event_ts_ms, event_ts_ms + 30_000)
        trades_60s_raw = _trades_in_window(all_trades, event_ts_ms, event_ts_ms + 60_000)
        trades_30s = [(t["side"], t["px"], t["sz"]) for t in trades_30s_raw]
        trades_60s = [(t["side"], t["px"], t["sz"]) for t in trades_60s_raw]

        rec = compute_per_event_features(
            c, bbo_snaps, l2_snaps, trades_30s, trades_60s, candles, horizons
        )
        if rec is None:
            n_no_entry += 1
            continue
        events.append(rec)
    print(f"  Per-event records: {len(events)}")
    print(f"  Skipped (no BBO):   {n_no_bbo}")
    print(f"  Skipped (no L2):    {n_no_l2}")
    print(f"  Skipped (no candle):{n_no_candles}")
    print(f"  Skipped (no entry): {n_no_entry}")
    if not events:
        print("No events to evaluate. Exiting.")
        return 1

    # Playbook filters
    playbooks = [
        ("generic", lambda ev, h: True),
        ("persistent_bid_heavy",
         lambda ev, h: ev["persistence_direction"] == "bid_heavy" and ev["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS),
        ("persistent_ask_heavy",
         lambda ev, h: ev["persistence_direction"] == "ask_heavy" and ev["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS),
        ("stale_book", lambda ev, h: ev["stale_book"]),
        ("flow_amplifies_30s", lambda ev, h: ev["flow_30s_label"] == "amplifies"),
        ("flow_fades_30s", lambda ev, h: ev["flow_30s_label"] == "fades"),
        ("flow_amplifies_60s", lambda ev, h: ev["flow_60s_label"] == "amplifies"),
        ("flow_fades_60s", lambda ev, h: ev["flow_60s_label"] == "fades"),
        ("book_absorbed_30s", lambda ev, h: ev["book_30s_label"] == "absorbed"),
        ("book_amplified_30s", lambda ev, h: ev["book_30s_label"] == "amplified"),
        ("persistent_bid_heavy_AND_flow_fades_30s",
         lambda ev, h: (ev["persistence_direction"] == "bid_heavy"
                        and ev["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS
                        and ev["flow_30s_label"] == "fades")),
    ]

    results: list[dict] = []
    print()
    print("=" * 78)
    print("RESULTS — per symbol, per horizon, per playbook")
    print("=" * 78)
    header = (
        f"  {'sym':<5} {'horizon':>7} {'playbook':<40} {'n':>4} "
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
                    f"  {sym:<5} {h:>5}m {pb_name:<40} {v.n:>4} "
                    f"{v.win_rate*100:>5.1f}% {v.avg_pnl_pct:>+8.4f} "
                    f"{v.median_pnl_pct:>+8.4f} {pf_str:>7} "
                    f"{v.top_win_share*100:>5.1f}%  {verdict}"
                )
                results.append(asdict(v))

    # Coverage stats
    n_btc = sum(1 for e in events if e["symbol"] == "BTC")
    n_eth = sum(1 for e in events if e["symbol"] == "ETH")
    coverage = {
        "btc_events": n_btc,
        "eth_events": n_eth,
        "btc_persistent_bid_heavy": sum(
            1 for e in events
            if e["symbol"] == "BTC" and e["persistence_direction"] == "bid_heavy"
            and e["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS
        ),
        "btc_persistent_ask_heavy": sum(
            1 for e in events
            if e["symbol"] == "BTC" and e["persistence_direction"] == "ask_heavy"
            and e["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS
        ),
        "eth_persistent_bid_heavy": sum(
            1 for e in events
            if e["symbol"] == "ETH" and e["persistence_direction"] == "bid_heavy"
            and e["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS
        ),
        "eth_persistent_ask_heavy": sum(
            1 for e in events
            if e["symbol"] == "ETH" and e["persistence_direction"] == "ask_heavy"
            and e["persistence_seconds"] >= PERSISTENCE_MIN_SECONDS
        ),
        "btc_stale_book": sum(1 for e in events if e["symbol"] == "BTC" and e["stale_book"]),
        "eth_stale_book": sum(1 for e in events if e["symbol"] == "ETH" and e["stale_book"]),
    }

    # Write JSON
    RESULTS_JSON_PATH.write_text(
        json.dumps(
            {
                "horizons": list(horizons),
                "symbols": list(symbols),
                "constants": {
                    "BBO_HEAVY_THRESHOLD": BBO_HEAVY_THRESHOLD,
                    "FLOW_NEUTRAL_BAND": FLOW_NEUTRAL_BAND,
                    "PERSISTENCE_MIN_SECONDS": PERSISTENCE_MIN_SECONDS,
                    "L2_LEVELS": L2_LEVELS,
                    "SPREAD_WIDEN_FACTOR": SPREAD_WIDEN_FACTOR,
                    "STALE_MID_DRIFT_PCT": STALE_MID_DRIFT_PCT,
                    "PROMOTION_N": PROMOTION_N,
                    "PROMOTION_PF": PROMOTION_PF,
                    "PROMOTION_TOP_WIN_SHARE": PROMOTION_TOP_WIN_SHARE,
                },
                "coverage": coverage,
                "n_events": len(events),
                "n_skipped": {
                    "no_bbo": n_no_bbo,
                    "no_l2": n_no_l2,
                    "no_candles": n_no_candles,
                    "no_entry": n_no_entry,
                },
                "verdicts": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote JSON: {RESULTS_JSON_PATH.name}")

    # Write Markdown summary
    _write_summary_md(results, events, symbols, horizons, coverage, n_no_bbo, n_no_l2, n_no_candles, n_no_entry)
    print(f"Wrote MD:   {SUMMARY_MD_PATH.name}")
    print()
    print("DONE. Research-only backtest. No execution touched.")
    return 0


def _write_summary_md(
    results: list[dict],
    events: list[dict],
    symbols: tuple[str, ...],
    horizons: tuple[int, ...],
    coverage: dict[str, int],
    n_no_bbo: int,
    n_no_l2: int,
    n_no_candles: int,
    n_no_entry: int,
) -> None:
    lines: list[str] = []
    lines.append("# Book-Persistence / Stale-Book / Trade-Flow Filter — Summary")
    lines.append("")
    lines.append("Research only. BTC/ETH. No execution wiring. Per-symbol reports only.")
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
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- Total events evaluated: {len(events)}")
    for k, v in coverage.items():
        lines.append(f"- {k}: {v}")
    lines.append(f"- Skipped (no BBO): {n_no_bbo}")
    lines.append(f"- Skipped (no L2): {n_no_l2}")
    lines.append(f"- Skipped (no candle): {n_no_candles}")
    lines.append(f"- Skipped (no entry): {n_no_entry}")
    lines.append("")
    lines.append("## How to read")
    lines.append("")
    lines.append("- `generic` = control, no filter.")
    lines.append("- `persistent_bid_heavy` / `persistent_ask_heavy` = BBO has been on the same side for at least 30s before the cascade.")
    lines.append("- `stale_book` = spread widened (>1.5x median over 5m) AND mid has not moved (<0.01% drift) in the 5m before the cascade.")
    lines.append("- `flow_amplifies_30s` / `flow_60s` = post-event trade flow is in the same direction as the cascade (book is amplifying).")
    lines.append("- `flow_fades_30s` / `flow_60s` = post-event trade flow is opposite the cascade (book is fighting back).")
    lines.append("- `book_absorbed_30s` / `book_amplified_30s` = post-event BBO imbalance shifted against / with the cascade direction.")
    lines.append("- Returns are in **percent units** (e.g. `+0.32` means `+0.32%`).")
    lines.append("")
    SUMMARY_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

"""
HyphyLiquid - cascade event clustering.

The liquidation detector (LiquidationDetector) emits one event per
qualified burst it finds in the public trade feed. In practice, a
single liquidation cascade (e.g. one forced-margin-event sweeping
through the book) shows up as MANY events over a few seconds because
the detector fires on overlapping burst patterns.

Per the BTC/ETH strategy sweep (docs/2026-08-02-RESEARCH-btc-eth-
hyperliquid-strategy-sweep.md, "Group adjacent liquidations within
30-120 s into one cascade; allow only one entry per cascade.") and
the spec build order Task 2 ("BTC side A, 20 events within 2 seconds
-> one cascade event. Keep total notional, max confidence, fill
count, event VWAP, start/end timestamp."), this module clusters raw
events into canonical cascades.

Algorithm:
- Group by (symbol, side) — cascade direction is the side of the
  forced flow.
- Within a group, sort by ts. Merge adjacent events whose ts
  difference is <= time_window_s. Default 60s; spec range 30-120s.
- For each cluster, output:
    symbol, side, start_ts, end_ts
    total_notional  = sum
    n_fills         = sum
    event_vwap      = notional-weighted average of price_avg
    max_confidence  = max
    n_events        = count of source events in the cluster
    duration_ms     = (end_ts - start_ts) in ms
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
from typing import Any


def _parse_ts(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cluster_events(events: list[dict], time_window_s: int = 60) -> list[dict]:
    """Group raw liquidation events into cascades.

    Args:
        events: list of dicts with at least ts, symbol, side,
            total_notional, n_fills, price_avg, confidence.
        time_window_s: events within this many seconds of each other
            (same symbol + same side) are merged into one cascade.
    Returns:
        list of cascade dicts, one per (symbol, side) cluster.
    """
    if not events:
        return []

    cascades: list[dict] = []
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ev in events:
        sym = ev.get("symbol")
        side = ev.get("side")
        ts = ev.get("ts")
        if not sym or not side or not ts:
            continue
        by_key[(str(sym), str(side))].append(ev)

    for (sym, side), grouped_events in by_key.items():
        sorted_events = sorted(grouped_events, key=lambda e: e.get("ts", ""))
        current: dict[str, Any] | None = None
        window_dt: datetime | None = None

        for ev in sorted_events:
            ts = ev.get("ts")
            if not ts:
                continue
            notional = float(ev.get("total_notional", 0) or 0)
            n_fills = int(ev.get("n_fills", 0) or 0)
            price_avg = float(ev.get("price_avg", 0) or 0)
            confidence = float(ev.get("confidence", 0) or 0)

            ev_dt = _parse_ts(ts)
            if current is None:
                current = _new_cluster(sym, side, ts, notional, n_fills, price_avg, confidence)
                window_dt = ev_dt
                continue

            gap_s = (ev_dt - window_dt).total_seconds() if window_dt else float("inf")
            if gap_s <= time_window_s:
                # Extend cluster. VWAP math: track total size = sum_i(notional_i/price_i).
                current["end_ts"] = ts
                current["total_notional"] += notional
                current["n_fills"] += n_fills
                current["total_size"] += (notional / price_avg) if price_avg > 0 else 0.0
                current["max_confidence"] = max(current["max_confidence"], confidence)
                current["n_events"] += 1
                window_dt = ev_dt
            else:
                cascades.append(_finalize(current))
                current = _new_cluster(sym, side, ts, notional, n_fills, price_avg, confidence)
                window_dt = ev_dt

        if current is not None:
            cascades.append(_finalize(current))

    return sorted(cascades, key=lambda c: c.get("start_ts", ""))


def _new_cluster(
    symbol: str,
    side: str,
    ts: str,
    notional: float,
    n_fills: int,
    price_avg: float,
    confidence: float,
) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "start_ts": ts,
        "end_ts": ts,
        "total_notional": notional,
        "n_fills": n_fills,
        # For VWAP math: cluster size (in coin) = sum_i(notional_i / price_i).
        # We track the running sum; finalize divides total_notional by
        # total_size to get the true VWAP. The old (wrong) formula
        # weighted by notional which biased toward higher-price sub-
        # events. See test_cascade_cluster.py::test_vwap_math for the
        # worked example.
        "total_size": (notional / price_avg) if price_avg > 0 else 0.0,
        "max_confidence": confidence,
        "n_events": 1,
    }


def _finalize(c: dict) -> dict:
    notional = c["total_notional"]
    size = c.get("total_size", 0.0)
    # True VWAP = sum(price * size) / sum(size) = total_notional / total_size.
    # When the cluster has 1 event, this is the same as the event's
    # price_avg (sanity check). When it has many, this is the right
    # size-weighted average across all fills.
    if size > 0:
        event_vwap = notional / size
    else:
        event_vwap = 0.0
    out = {
        "symbol": c["symbol"],
        "side": c["side"],
        "start_ts": c["start_ts"],
        "end_ts": c["end_ts"],
        "total_notional": notional,
        "n_fills": c["n_fills"],
        "event_vwap": event_vwap,
        "max_confidence": c["max_confidence"],
        "n_events": c["n_events"],
    }
    # Duration in ms
    try:
        start = _parse_ts(c["start_ts"])
        end = _parse_ts(c["end_ts"])
        out["duration_ms"] = int((end - start).total_seconds() * 1000)
    except (TypeError, ValueError):
        out["duration_ms"] = 0
    return out

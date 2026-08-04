"""
HyphyLiquid - event-level feature store.

The liquidation monitor writes one line to data/liquidations.jsonl per
detected event. This module enriches each event with the book and
asset-ctx state at the moment of detection and writes the result to
data/event_features.jsonl.

Per the BTC/ETH strategy sweep (docs/2026-08-02-RESEARCH-btc-eth-
hyperliquid-strategy-sweep.md, line 162), the spec wants at-event
snapshots of:
  - event_vwap (already in the event itself)
  - pre / post price (post is filled later by a backfill walker)
  - OI before / after (we snapshot OI at detection; pre is the prior asset_ctx)
  - funding / predicted funding
  - bbo spread
  - top-of-book imbalance

This module captures the at-detection snapshot. Post-event features
(return over the next 1m / 5m / 30m, eventual fill price) need to be
backfilled by a separate process that walks event_features.jsonl
forward in time.

The data paths are read-only; we never modify the source l2book or
asset_ctx files, only read the latest line per symbol/date.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EVENT_FEATURES_PATH = DATA_DIR / "event_features.jsonl"


def _latest_line(path: Path) -> dict | None:
    """Return the parsed JSON of the last non-empty line in path, or None."""
    if not path.exists():
        return None
    try:
        # Read last 4KB - enough for a single record on any channel
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def _line_nearest_ts(path: Path, target_ts_ms: int) -> dict | None:
    """Return the parsed JSON whose source-ts is closest to target_ts_ms.

    Walks the file in O(n). Used by the historical backfill so each
    event gets a snapshot from the SAME point in time, not the file's
    most-recent line (which is "now" and would leak future state into
    the backtest).
    """
    if not path.exists():
        return None
    best: dict | None = None
    best_dist: int | None = None
    try:
        # Stream through the file line by line. Keep the closest match.
        # For 30-40MB files this is ~1-2s per file in Python; acceptable
        # for a one-shot backfill.
        for line in path.open("r", encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Look for a ts field in standard places
            ts_ms: int | None = None
            if isinstance(rec, dict):
                if "ts" in rec and isinstance(rec["ts"], (int, float)):
                    ts_ms = int(rec["ts"])
                elif isinstance(rec.get("payload"), dict):
                    p = rec["payload"]
                    if isinstance(p.get("time"), (int, float)):
                        ts_ms = int(p["time"])
                elif "poll_ts" in rec and isinstance(rec["poll_ts"], str):
                    try:
                        dt = datetime.fromisoformat(rec["poll_ts"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        ts_ms = int(dt.timestamp() * 1000)
                    except (TypeError, ValueError):
                        ts_ms = None
            if ts_ms is None:
                continue
            dist = abs(ts_ms - target_ts_ms)
            if best_dist is None or dist < best_dist:
                best = rec
                best_dist = dist
    except OSError:
        return None
    return best


def _date_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _canonical_symbol(symbol: str) -> str:
    if ":" not in symbol:
        return symbol.upper()
    dex, market = symbol.split(":", 1)
    return f"{dex.lower()}:{market.upper()}"


def _file_stem(symbol: str) -> str:
    return _canonical_symbol(symbol).lower().replace(":", "_")


def _symbol_date_path(root: Path, sym: str, ts_ms: int, ext: str = "jsonl") -> Path:
    return root / f"{_file_stem(sym)}_{_date_str(ts_ms)}.{ext}"


def _bbo_from_l2book(l2book_payload: dict | None) -> dict:
    """Extract bbo + top-of-book imbalance from an l2book payload.

    Hyperliquid l2book payload shape (after our WS handler wraps it):
      {"coin": "BTC", "levels": [[bids...], [asks...]], "ts": <ms>}
    Each level: {"px": str, "sz": str, "n": int}
    Top-of-book: bids[0] = best bid, asks[0] = best ask.
    Imbalance = (sum(top-3 bid sz) - sum(top-3 ask sz)) / total.
    """
    if not isinstance(l2book_payload, dict):
        return {"bbo_spread": None, "bbo_spread_bps": None, "top_book_imbalance": None}
    levels = l2book_payload.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        return {"bbo_spread": None, "bbo_spread_bps": None, "top_book_imbalance": None}
    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        return {"bbo_spread": None, "bbo_spread_bps": None, "top_book_imbalance": None}
    try:
        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        spread = best_ask - best_bid
        # mid for bps normalization
        mid = (best_ask + best_bid) / 2.0
        spread_bps = (spread / mid) * 10_000.0 if mid > 0 else None
        # top-3 imbalance
        top_bid_sz = sum(float(b.get("sz", 0)) for b in bids[:3])
        top_ask_sz = sum(float(a.get("sz", 0)) for a in asks[:3])
        total = top_bid_sz + top_ask_sz
        imbalance = (top_bid_sz - top_ask_sz) / total if total > 0 else None
    except (KeyError, ValueError, TypeError, IndexError):
        return {"bbo_spread": None, "bbo_spread_bps": None, "top_book_imbalance": None}
    return {
        "bbo_spread": spread,
        "bbo_spread_bps": spread_bps,
        "top_book_imbalance": imbalance,
    }


def _asset_ctx_features(record: dict | None) -> dict:
    """Pull oi / funding / predicted funding from an asset_ctx poll record.

    Our poll_asset_ctx record shape:
      {"poll_ts": "...", "symbol": "BTC", "context": {...}, "predicted": {...}}
    context keys: markPx, oraclePx, funding, openInterest, ...
    predicted["HlPerp"] keys: fundingRate, ...
    """
    if not isinstance(record, dict):
        return {"oi": None, "funding": None, "predicted_funding": None, "mark_px": None}
    ctx = record.get("context") if isinstance(record.get("context"), dict) else {}
    pred = record.get("predicted") if isinstance(record.get("predicted"), dict) else {}
    pred_hl = pred.get("HlPerp") if isinstance(pred.get("HlPerp"), dict) else {}
    return {
        "oi": ctx.get("openInterest"),
        "funding": ctx.get("funding"),
        "predicted_funding": pred_hl.get("fundingRate"),
        "mark_px": ctx.get("markPx"),
    }


def snapshot_event_features(event: dict, nearest: bool = True) -> dict:
    """Build a feature dict for a detected liquidation event.

    Args:
        event: dict matching liquidation_monitor._ev_to_record shape
            (ts, symbol, side, total_notional, n_fills, price_avg,
             duration_ms, confidence, reason).
        nearest: if True (default for backfill / cluster scripts), pick
            the l2book + asset_ctx record whose source-ts is closest to
            the event ts. If False, use the most-recent record
            (suitable for the live monitor at detection time, where
            "now" IS the most recent).

    For live use the monitor calls snapshot_event_features(event)
    which uses _latest_line (the most recent snapshot = "now"). For
    historical backfill the cluster script passes nearest=True so
    each event gets a snapshot from its own time, not from "now."
    """
    sym = _canonical_symbol(event.get("symbol", "?"))
    ts_str = event.get("ts", "")
    try:
        ts_dt = datetime.fromisoformat(ts_str)
    except (TypeError, ValueError):
        ts_dt = datetime.now(timezone.utc)
    if ts_dt.tzinfo is None:
        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
    ts_ms = int(ts_dt.timestamp() * 1000)

    l2_path = _symbol_date_path(DATA_DIR / "ws_l2book", sym, ts_ms)
    ctx_path = _symbol_date_path(DATA_DIR / "asset_ctx", sym, ts_ms)

    if nearest:
        l2_record = _line_nearest_ts(l2_path, ts_ms)
        ctx_record = _line_nearest_ts(ctx_path, ts_ms)
    else:
        l2_record = _latest_line(l2_path)
        ctx_record = _latest_line(ctx_path)

    l2_payload = l2_record.get("payload") if isinstance(l2_record, dict) else None
    book_features = _bbo_from_l2book(l2_payload)

    ctx_features = _asset_ctx_features(ctx_record)

    # Compute event VWAP sanity-check vs event-reported price_avg.
    # total_notional / n_fills is average fill notional, not VWAP.
    event_vwap = event.get("price_avg")
    try:
        n = int(event.get("n_fills", 0)) or 0
        notional = float(event.get("total_notional", 0)) or 0.0
        avg_fill_notional = (notional / n) if n > 0 else None
    except (TypeError, ValueError):
        avg_fill_notional = None

    return {
        # event identity
        "event_ts": ts_str,
        "event_ts_ms": ts_ms,
        "symbol": sym,
        "side": event.get("side"),
        # event primitives
        "event_vwap": event_vwap,
        "vwap_check": event_vwap,
        "avg_fill_notional": avg_fill_notional,
        "total_notional": event.get("total_notional"),
        "n_fills": event.get("n_fills"),
        "duration_ms": event.get("duration_ms"),
        "confidence": event.get("confidence"),
        "reason": event.get("reason"),
        # book state at detection
        **book_features,
        # asset-ctx state at detection
        **ctx_features,
        # source timestamps (for staleness checks)
        "l2_source_ts": l2_payload.get("ts") if isinstance(l2_payload, dict) else None,
        "ctx_source_poll_ts": (ctx_record.get("poll_ts") if isinstance(ctx_record, dict) else None),
        # post-event features filled by backfill walker; null for now
        "post_1m_return": None,
        "post_5m_return": None,
        "post_15m_return": None,
        "post_30m_return": None,
    }


def write_event_features(event: dict, path: Path = EVENT_FEATURES_PATH) -> dict:
    """Snapshot, write, and return the feature dict."""
    features = snapshot_event_features(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(features) + "\n")
    return features

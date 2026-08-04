"""
HyphyLiquid - cluster raw liquidation events into canonical cascades,
and enrich each cascade with book + asset-ctx features at detection.

Reads:  data/liquidations.jsonl (raw per-detector events)
Writes: data/cascades.jsonl (one row per cascade: cluster fields + features)

This is the spec build order Task 2 ("Patch dedupe/clustering next")
plus the historical backfill (raw events from earlier sessions had no
features; the backtest will need them).

Run:
    python scripts/build_cascades.py [--time-window 60]
"""
import argparse
import json
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.cascade_cluster import cluster_events
from src.strategy.event_features import (
    _bbo_from_l2book,
    _asset_ctx_features,
    _file_stem,
    EVENT_FEATURES_PATH,
)

LIQ_PATH = PROJECT_ROOT / "data" / "liquidations.jsonl"
CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"


def _load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class SnapshotIndex:
    """In-memory index of {ts_ms: record} built ONCE per file.
    Per-key lookup is O(log n) via bisect. Cuts build_cascades time
    from O(events * file_size) to O(file_size + events * log n)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.timestamps: list[int] = []
        self.records: list[dict] = []
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _extract_ts(rec)
                if ts is None:
                    continue
                self.timestamps.append(ts)
                self.records.append(rec)
        # Sort by ts so bisect works. Use key=ts only because the WS feed
        # can deliver multiple records at the same millisecond, and dict
        # records don't have a natural ordering for tie-breaking.
        if self.timestamps:
            paired = sorted(zip(self.timestamps, self.records), key=lambda x: x[0])
            self.timestamps = [t for t, _ in paired]
            self.records = [r for _, r in paired]

    def nearest(self, target_ms: int) -> dict | None:
        nearest = self.nearest_with_delta(target_ms)
        return nearest[0] if nearest else None

    def nearest_with_delta(self, target_ms: int) -> tuple[dict, float] | None:
        if not self.timestamps:
            return None
        idx = bisect_left(self.timestamps, target_ms)
        candidates = []
        if idx < len(self.timestamps):
            candidates.append((self.timestamps[idx], self.records[idx]))
        if idx > 0:
            candidates.append((self.timestamps[idx - 1], self.records[idx - 1]))
        if not candidates:
            return None
        candidates.sort(key=lambda x: abs(x[0] - target_ms))
        ts, rec = candidates[0]
        return rec, (ts - target_ms) / 1000.0


def _extract_ts(rec: dict) -> int | None:
    if not isinstance(rec, dict):
        return None
    if "ts" in rec and isinstance(rec["ts"], (int, float)):
        return int(rec["ts"])
    if isinstance(rec.get("payload"), dict):
        p = rec["payload"]
        if isinstance(p.get("time"), (int, float)):
            return int(p["time"])
        if isinstance(p.get("ts"), (int, float)):
            return int(p["ts"])
        if isinstance(p.get("t"), (int, float)):
            return int(p["t"])
    if "poll_ts" in rec and isinstance(rec["poll_ts"], str):
        try:
            dt = datetime.fromisoformat(rec["poll_ts"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (TypeError, ValueError):
            return None
    return None


def _date_str_from_iso(ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _build_indexes(symbol_dates: Iterable[tuple[str, str]]) -> tuple[dict, dict]:
    """Build {(symbol, date): SnapshotIndex} for l2book and asset_ctx."""
    l2_idx: dict[tuple[str, str], SnapshotIndex] = {}
    ctx_idx: dict[tuple[str, str], SnapshotIndex] = {}
    for sym, date_str in sorted(set(symbol_dates)):
        stem = _file_stem(sym)
        l2_path = PROJECT_ROOT / "data" / "ws_l2book" / f"{stem}_{date_str}.jsonl"
        ctx_path = PROJECT_ROOT / "data" / "asset_ctx" / f"{stem}_{date_str}.jsonl"
        l2_idx[(sym, date_str)] = SnapshotIndex(l2_path)
        ctx_idx[(sym, date_str)] = SnapshotIndex(ctx_path)
    return l2_idx, ctx_idx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--time-window",
        type=int,
        default=60,
        help="Cluster window in seconds. Default 60. Spec range 30-120.",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip the per-cascade event_features enrichment (faster).",
    )
    parser.add_argument(
        "--only-symbol",
        choices=("BTC", "ETH", "SOL", "HYPE", "DOGE", "BNB", "xyz:GOLD", "xyz:SILVER"),
        help="Only cluster events for this symbol.",
    )
    parser.add_argument(
        "--max-snapshot-lag",
        type=int,
        default=120,
        help="Max absolute seconds from cascade to nearest l2/ctx snapshot. Older/future snapshots become null.",
    )
    args = parser.parse_args()

    print("HyphyLiquid - Build Cascades")
    print("=" * 60)

    events = _load_events(LIQ_PATH)
    print(f"\nLoaded {len(events)} raw events from {LIQ_PATH.name}")
    if args.only_symbol:
        events = [e for e in events if e.get("symbol") == args.only_symbol]
        print(f"  filtered to {len(events)} {args.only_symbol} events")

    cascades = cluster_events(events, time_window_s=args.time_window)
    print(f"  -> {len(cascades)} cascades after clustering (window={args.time_window}s)")

    by_sym: dict[str, int] = {}
    by_side: dict[str, int] = {}
    for c in cascades:
        by_sym[c["symbol"]] = by_sym.get(c["symbol"], 0) + 1
        by_side[c["side"]] = by_side.get(c["side"], 0) + 1
    print(f"  by symbol: {by_sym}")
    print(f"  by side:   {by_side}")

    if args.no_enrich:
        CASCADES_PATH.write_text(
            "\n".join(json.dumps(c) for c in cascades) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {len(cascades)} cascades to {CASCADES_PATH.name} (no enrichment)")
    else:
        # Build per-file indexes ONCE, then bisect per cascade.
        symbol_dates = {
            (c["symbol"], date_str)
            for c in cascades
            if c.get("symbol") and (date_str := _date_str_from_iso(c.get("start_ts")))
        }
        print(f"\nBuilding snapshot indexes for {len(symbol_dates)} symbol-date files...")
        l2_idx, ctx_idx = _build_indexes(symbol_dates)
        for (sym, date_str), idx in l2_idx.items():
            print(f"  l2  {sym} {date_str}: {len(idx.timestamps)} records")
        for (sym, date_str), idx in ctx_idx.items():
            print(f"  ctx {sym} {date_str}: {len(idx.timestamps)} records")

        written = 0
        skipped = 0
        with CASCADES_PATH.open("w", encoding="utf-8") as f:
            for c in cascades:
                sym = c.get("symbol")
                try:
                    ts_dt = datetime.fromisoformat(c["start_ts"])
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                ts_ms = int(ts_dt.timestamp() * 1000)
                date_str = ts_dt.strftime("%Y-%m-%d")

                l2_index = l2_idx.get((sym, date_str))
                ctx_index = ctx_idx.get((sym, date_str))
                l2_nearest = l2_index.nearest_with_delta(ts_ms) if l2_index else None
                ctx_nearest = ctx_index.nearest_with_delta(ts_ms) if ctx_index else None
                l2_rec = (
                    l2_nearest[0]
                    if l2_nearest and abs(l2_nearest[1]) <= args.max_snapshot_lag
                    else None
                )
                ctx_rec = (
                    ctx_nearest[0]
                    if ctx_nearest and abs(ctx_nearest[1]) <= args.max_snapshot_lag
                    else None
                )
                l2_delta_s = l2_nearest[1] if l2_nearest and l2_rec else None
                ctx_delta_s = ctx_nearest[1] if ctx_nearest and ctx_rec else None
                l2_payload = l2_rec.get("payload") if isinstance(l2_rec, dict) else None
                book_features = _bbo_from_l2book(l2_payload)
                ctx_features = _asset_ctx_features(ctx_rec)

                # VWAP sanity check and average fill notional are distinct.
                try:
                    n = int(c.get("n_fills", 0)) or 0
                    notional = float(c.get("total_notional", 0)) or 0.0
                    avg_fill_notional = (notional / n) if n > 0 else None
                except (TypeError, ValueError):
                    avg_fill_notional = None

                merged = {
                    **c,
                    "event_ts": c["start_ts"],
                    "event_ts_ms": ts_ms,
                    "vwap_check": c.get("event_vwap"),
                    "avg_fill_notional": avg_fill_notional,
                    "bbo_source_ts": (
                        l2_payload.get("ts") or l2_payload.get("time")
                        if isinstance(l2_payload, dict) else None
                    ),
                    "l2_delta_s": round(l2_delta_s, 3) if l2_delta_s is not None else None,
                    **book_features,
                    **ctx_features,
                    "ctx_delta_s": round(ctx_delta_s, 3) if ctx_delta_s is not None else None,
                    "post_1m_return": None,
                    "post_5m_return": None,
                    "post_15m_return": None,
                    "post_30m_return": None,
                }
                f.write(json.dumps(merged) + "\n")
                written += 1
        print(
            f"\nWrote {written} enriched cascades to {CASCADES_PATH.name} "
            f"(skipped {skipped} on failure)"
        )

    if cascades:
        sample = cascades[0]
        print("\nFirst cascade (raw cluster fields):")
        for k, v in sample.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

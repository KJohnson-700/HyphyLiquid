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
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.cascade_cluster import cluster_events
from src.strategy.event_features import snapshot_event_features

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
        choices=("BTC", "ETH", "SOL", "HYPE"),
        help="Only cluster events for this symbol.",
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

    # Distribution
    by_sym: dict[str, int] = {}
    by_side: dict[str, int] = {}
    for c in cascades:
        by_sym[c["symbol"]] = by_sym.get(c["symbol"], 0) + 1
        by_side[c["side"]] = by_side.get(c["side"], 0) + 1
    print(f"  by symbol: {by_sym}")
    print(f"  by side:   {by_side}")

    # Enrich
    if args.no_enrich:
        CASCADES_PATH.write_text(
            "\n".join(json.dumps(c) for c in cascades) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {len(cascades)} cascades to {CASCADES_PATH.name} (no enrichment)")
    else:
        written = 0
        skipped = 0
        with CASCADES_PATH.open("w", encoding="utf-8") as f:
            for c in cascades:
                # Build a minimal synthetic event for the feature writer:
                # it expects ts, symbol, side, total_notional, n_fills,
                # price_avg, duration_ms, confidence, reason.
                synth_event = {
                    "ts": c["start_ts"],
                    "symbol": c["symbol"],
                    "side": c["side"],
                    "total_notional": c["total_notional"],
                    "n_fills": c["n_fills"],
                    "price_avg": c["event_vwap"],
                    "duration_ms": c["duration_ms"],
                    "confidence": c["max_confidence"],
                    "reason": f"cluster of {c['n_events']} raw events",
                }
                try:
                    features = snapshot_event_features(synth_event)
                except Exception as e:
                    print(f"  [warn] feature snapshot failed for cascade {c.get('start_ts')}: {e}")
                    skipped += 1
                    continue
                # Merge cluster fields + features (features win on conflict)
                merged = {**c, **features}
                f.write(json.dumps(merged) + "\n")
                written += 1
        print(
            f"\nWrote {written} enriched cascades to {CASCADES_PATH.name} "
            f"(skipped {skipped} on feature failure)"
        )

    # Sample
    if cascades:
        sample = cascades[0]
        print("\nFirst cascade (raw cluster fields):")
        for k, v in sample.items():
            print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run research-only event-anchored range grid backtest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_fade_or_follow_backtest import _load_candles, _load_cascades  # noqa: E402
from src.strategy.event_features import _canonical_symbol  # noqa: E402
from src.strategy.grid_backtest import (  # noqa: E402
    GRID_RESEARCH_SYMBOLS,
    GridConfig,
    run_event_range_grid,
    summarize_grid_trades,
)

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
OUT_DIR = PROJECT_ROOT / "data"


def _print_summary(summary: dict[str, dict]) -> None:
    print()
    print("=" * 96)
    print("event_range_grid results")
    print("=" * 96)
    if not summary:
        print("  (no trades)")
        return
    print(
        f"  {'bucket':<48} {'n':>4} {'WR%':>6} {'avg_net%':>9} "
        f"{'med_net%':>9} {'PF':>7} {'avgLvls':>8}  warning"
    )
    print("  " + "-" * 94)
    for key, row in sorted(summary.items()):
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        warning = "SAMPLE TOO SMALL" if row["n"] < 10 else ""
        print(
            f"  {key:<48} {row['n']:>4} {row['win_rate'] * 100:>5.1f}% "
            f"{row['avg_net_return_pct']:>+8.4f} {row['median_net_return_pct']:>+8.4f} "
            f"{pf_str:>7} {row['avg_levels_filled']:>8.2f}  {warning}"
        )


def _out_suffix(symbol: str | None, side: str | None) -> str:
    parts = ["grid_event_range"]
    if symbol:
        parts.append(symbol.lower().replace(":", "_"))
    if side:
        parts.append(f"side_{side.lower()}")
    return "_".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=sorted(GRID_RESEARCH_SYMBOLS))
    parser.add_argument("--side", choices=("A", "B"))
    parser.add_argument("--band-period", type=int, default=20)
    parser.add_argument("--stdev", type=float, default=2.0)
    parser.add_argument("--band-buckets", default="normal,wide")
    parser.add_argument("--grid-spacing-bps", type=float, default=10.0)
    parser.add_argument("--max-levels", type=int, default=3)
    parser.add_argument("--stop-buffer-bps", type=float, default=10.0)
    parser.add_argument("--max-hold", type=int, default=60)
    parser.add_argument("--max-entry-lag", type=int, default=2)
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    args = parser.parse_args()

    print("HyphyLiquid - Event Range Grid Backtest")
    print("=" * 60)

    allowed = {_canonical_symbol(args.symbol)} if args.symbol else set(GRID_RESEARCH_SYMBOLS)
    cascades = [
        c for c in _load_cascades(CASCADES_PATH)
        if _canonical_symbol(str(c.get("symbol", ""))) in allowed
    ]
    if args.side:
        cascades = [c for c in cascades if c.get("side") == args.side]
    symbols = {_canonical_symbol(str(c.get("symbol", ""))) for c in cascades}
    print(f"\nLoaded {len(cascades)} research cascades")
    candles_by_symbol = {}
    for sym in sorted(symbols):
        candles_by_symbol[sym] = _load_candles(sym)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles loaded")

    buckets = tuple(x.strip() for x in args.band_buckets.split(",") if x.strip())
    config = GridConfig(
        band_period=args.band_period,
        stdev_mult=args.stdev,
        allowed_band_buckets=buckets,
        grid_spacing_bps=args.grid_spacing_bps,
        max_levels=args.max_levels,
        stop_buffer_bps=args.stop_buffer_bps,
        max_hold_minutes=args.max_hold,
        max_entry_lag_minutes=args.max_entry_lag,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    trades = run_event_range_grid(cascades, candles_by_symbol, config)
    summary = summarize_grid_trades(trades)

    suffix = _out_suffix(args.symbol, args.side)
    trades_path = OUT_DIR / f"{suffix}_trades.jsonl"
    summary_path = OUT_DIR / f"{suffix}_summary.json"
    trades_path.write_text("\n".join(json.dumps(t.to_dict(), sort_keys=True) for t in trades) + ("\n" if trades else ""), encoding="utf-8")
    summary_path.write_text(
        json.dumps(
            {
                "config": config.__dict__,
                "cascades": len(cascades),
                "trades": len(trades),
                "summary": summary,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    _print_summary(summary)
    print(f"\nWrote {len(trades)} trades to {trades_path.name}")
    print(f"Wrote summary to {summary_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

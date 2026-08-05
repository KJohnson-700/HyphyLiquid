"""Sweep research-only event range grid parameters."""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from statistics import mean, median

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_fade_or_follow_backtest import _load_candles, _load_cascades  # noqa: E402
from src.strategy.event_features import _canonical_symbol  # noqa: E402
from src.strategy.grid_backtest import GRID_RESEARCH_SYMBOLS, GridConfig, run_event_range_grid  # noqa: E402

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
OUT_DIR = PROJECT_ROOT / "data"


def _parse_csv_floats(value: str) -> list[float]:
    return [float(v.strip()) for v in value.split(",") if v.strip()]


def _parse_csv_ints(value: str) -> list[int]:
    return [int(v.strip()) for v in value.split(",") if v.strip()]


def _parse_bucket_sets(value: str) -> list[tuple[str, ...]]:
    """Parse 'normal;wide;normal,wide' into bucket tuples."""
    sets = []
    for chunk in value.split(";"):
        buckets = tuple(v.strip() for v in chunk.split(",") if v.strip())
        if buckets:
            sets.append(buckets)
    return sets


def _profit_factor(values: list[float]) -> float | str:
    wins = sum(v for v in values if v > 0)
    losses = abs(sum(v for v in values if v < 0))
    if losses == 0:
        return "inf" if wins > 0 else 0.0
    return round(wins / losses, 4)


def _top_win_share(values: list[float]) -> float:
    wins = [v for v in values if v > 0]
    gross = sum(wins)
    if gross <= 0:
        return 0.0
    return round(max(wins) / gross, 4)


def _summarize_sweep(trades: list[dict], config: GridConfig, *, symbol: str | None, side: str | None) -> dict:
    values = [float(t["net_return_pct"]) for t in trades]
    wins = [v for v in values if v > 0]
    exit_counts: dict[str, int] = {}
    band_counts: dict[str, int] = {}
    for trade in trades:
        exit_counts[trade["exit_reason"]] = exit_counts.get(trade["exit_reason"], 0) + 1
        band_counts[trade["band_width_bucket"]] = band_counts.get(trade["band_width_bucket"], 0) + 1
    return {
        "symbol": symbol or "ALL",
        "side": side or "ALL",
        "n": len(values),
        "win_rate": round(len(wins) / len(values), 4) if values else 0.0,
        "avg_net_return_pct": round(mean(values), 4) if values else 0.0,
        "median_net_return_pct": round(median(values), 4) if values else 0.0,
        "profit_factor": _profit_factor(values),
        "top_win_share": _top_win_share(values),
        "avg_levels_filled": round(mean([int(t["levels_filled"]) for t in trades]), 4) if trades else 0.0,
        "exit_counts": exit_counts,
        "band_counts": band_counts,
        "band_buckets": list(config.allowed_band_buckets),
        "grid_spacing_bps": config.grid_spacing_bps,
        "max_levels": config.max_levels,
        "stop_buffer_bps": config.stop_buffer_bps,
        "max_hold_minutes": config.max_hold_minutes,
        "band_period": config.band_period,
        "stdev_mult": config.stdev_mult,
    }


def _passes_watch(row: dict, min_n: int) -> bool:
    pf = row["profit_factor"]
    pf_ok = pf == "inf" or (isinstance(pf, (int, float)) and pf > 1.5)
    return (
        row["n"] >= min_n
        and pf_ok
        and row["median_net_return_pct"] > 0
        and row["top_win_share"] <= 0.35
    )


def run_sweep(
    *,
    symbol: str | None,
    side: str | None,
    spacing_bps: list[float],
    max_levels: list[int],
    stop_buffers_bps: list[float],
    max_holds: list[int],
    bucket_sets: list[tuple[str, ...]],
    min_n: int,
) -> list[dict]:
    """Run a deterministic grid parameter sweep."""
    allowed = {_canonical_symbol(symbol)} if symbol else set(GRID_RESEARCH_SYMBOLS)
    cascades = [
        c for c in _load_cascades(CASCADES_PATH)
        if _canonical_symbol(str(c.get("symbol", ""))) in allowed
    ]
    if side:
        cascades = [c for c in cascades if c.get("side") == side]
    symbols = {_canonical_symbol(str(c.get("symbol", ""))) for c in cascades}
    candles_by_symbol = {sym: _load_candles(sym) for sym in sorted(symbols)}

    rows: list[dict] = []
    for buckets, spacing, levels, stop_buffer, hold in itertools.product(
        bucket_sets,
        spacing_bps,
        max_levels,
        stop_buffers_bps,
        max_holds,
    ):
        config = GridConfig(
            allowed_band_buckets=buckets,
            grid_spacing_bps=spacing,
            max_levels=levels,
            stop_buffer_bps=stop_buffer,
            max_hold_minutes=hold,
        )
        trades = [t.to_dict() for t in run_event_range_grid(cascades, candles_by_symbol, config)]
        row = _summarize_sweep(trades, config, symbol=symbol, side=side)
        row["watch_pass"] = _passes_watch(row, min_n)
        rows.append(row)
    rows.sort(
        key=lambda r: (
            not r["watch_pass"],
            -r["n"],
            -(float("inf") if r["profit_factor"] == "inf" else float(r["profit_factor"])),
            -r["median_net_return_pct"],
        )
    )
    return rows


def _print_top(rows: list[dict], top: int) -> None:
    print()
    print("=" * 118)
    print("event_range_grid sweep")
    print("=" * 118)
    print(
        f"  {'buckets':<14} {'spc':>5} {'lvls':>4} {'stop':>5} {'hold':>5} "
        f"{'n':>4} {'WR%':>6} {'avg%':>8} {'med%':>8} {'PF':>7} {'topWin':>7} {'watch':>6}"
    )
    print("  " + "-" * 116)
    for row in rows[:top]:
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        print(
            f"  {','.join(row['band_buckets']):<14} {row['grid_spacing_bps']:>5.1f} "
            f"{row['max_levels']:>4} {row['stop_buffer_bps']:>5.1f} {row['max_hold_minutes']:>5} "
            f"{row['n']:>4} {row['win_rate'] * 100:>5.1f}% {row['avg_net_return_pct']:>+7.4f} "
            f"{row['median_net_return_pct']:>+7.4f} {pf_str:>7} {row['top_win_share']:>7.1%} "
            f"{str(row['watch_pass']):>6}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=sorted(GRID_RESEARCH_SYMBOLS), default="HYPE")
    parser.add_argument("--side", choices=("A", "B"), default="B")
    parser.add_argument("--spacing-bps", default="5,10,15,25")
    parser.add_argument("--max-levels", default="1,2,3,4")
    parser.add_argument("--stop-buffers-bps", default="5,10,20,30")
    parser.add_argument("--max-holds", default="15,30,60,120")
    parser.add_argument("--bucket-sets", default="wide;normal,wide;normal")
    parser.add_argument("--min-n", type=int, default=20)
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    rows = run_sweep(
        symbol=args.symbol,
        side=args.side,
        spacing_bps=_parse_csv_floats(args.spacing_bps),
        max_levels=_parse_csv_ints(args.max_levels),
        stop_buffers_bps=_parse_csv_floats(args.stop_buffers_bps),
        max_holds=_parse_csv_ints(args.max_holds),
        bucket_sets=_parse_bucket_sets(args.bucket_sets),
        min_n=args.min_n,
    )
    suffix = f"grid_sweep_{args.symbol.lower().replace(':', '_')}_side_{args.side.lower()}"
    out = OUT_DIR / f"{suffix}.json"
    out.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    _print_top(rows, args.top)
    print(f"\nWrote {len(rows)} rows to {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

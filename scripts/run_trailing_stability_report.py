"""Check whether a trailing-stop candidate survives coverage/fold slicing.

This is analysis-only. It answers the specific question raised by the BTC
B-side trailing result: does the candidate hold when we compare a 120m sample
against the stricter 240m-mature subset, or was the apparent edge mostly a
coverage artifact?
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_fade_or_follow_backtest import _load_candles, _load_cascades  # noqa: E402
from src.strategy.fade_or_follow_backtest import run_backtest  # noqa: E402
from src.strategy.lane_backtest import TrailingExitTrade, apply_trailing_exits  # noqa: E402

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
OUT_DIR = PROJECT_ROOT / "data"


def _parse_csv_ints(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_csv_floats(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _dt(ts: str) -> datetime:
    out = datetime.fromisoformat(ts)
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out


def _pf(returns: list[float]) -> float | str:
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gross_loss = -sum(losses)
    if gross_loss <= 0:
        return "inf" if wins else 0.0
    return round(sum(wins) / gross_loss, 3)


def summarize_trailing_rows(trades: list[TrailingExitTrade]) -> dict:
    """Return compact stability metrics for one candidate slice."""
    returns = [t.net_return_pct for t in trades]
    r_values = [t.r_multiple for t in trades]
    wins = [r for r in returns if r > 0]
    initial_stops = sum(1 for t in trades if t.exit_reason == "initial_stop")
    trailing_stops = sum(1 for t in trades if t.exit_reason == "trailing_stop")
    activated = sum(1 for t in trades if t.exit_reason in {"trailing_stop", "timeout_trailing_active"})
    timeouts = sum(1 for t in trades if t.exit_reason.startswith("timeout"))
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "avg_net_return_pct": round(mean(returns), 4) if returns else 0.0,
        "median_net_return_pct": round(median(returns), 4) if returns else 0.0,
        "avg_r": round(mean(r_values), 4) if r_values else 0.0,
        "median_r": round(median(r_values), 4) if r_values else 0.0,
        "profit_factor": _pf(returns),
        "activation_rate": round(activated / len(trades), 4) if trades else 0.0,
        "initial_stop_rate": round(initial_stops / len(trades), 4) if trades else 0.0,
        "trailing_stop_rate": round(trailing_stops / len(trades), 4) if trades else 0.0,
        "timeout_rate": round(timeouts / len(trades), 4) if trades else 0.0,
        "avg_initial_stop_bps": round(mean(t.initial_stop_bps for t in trades), 4) if trades else 0.0,
        "first_event_ts": min((t.cascade_start_ts for t in trades), default=""),
        "last_event_ts": max((t.cascade_start_ts for t in trades), default=""),
    }


def _folds(trades: list[TrailingExitTrade], fold_count: int) -> list[list[TrailingExitTrade]]:
    ordered = sorted(trades, key=lambda t: _dt(t.cascade_start_ts))
    if fold_count <= 1 or not ordered:
        return [ordered]
    size = max(1, len(ordered) // fold_count)
    out: list[list[TrailingExitTrade]] = []
    for i in range(fold_count):
        start = i * size
        end = len(ordered) if i == fold_count - 1 else (i + 1) * size
        chunk = ordered[start:end]
        if chunk:
            out.append(chunk)
    return out


def _print_row(label: str, row: dict) -> None:
    pf = row["profit_factor"]
    pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>6}"
    print(
        f"  {label:<28} {row['n']:>4} {row['win_rate'] * 100:>5.1f}% "
        f"{row['avg_net_return_pct']:>+8.4f} {row['median_net_return_pct']:>+8.4f} "
        f"{row['avg_r']:>+7.3f} {pf_str:>7} {row['activation_rate'] * 100:>5.1f}% "
        f"{row['initial_stop_rate'] * 100:>7.1f}% {row['trailing_stop_rate'] * 100:>6.1f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("BTC", "ETH"), default="BTC")
    parser.add_argument("--side", choices=("A", "B"), default="B")
    parser.add_argument("--variant", default="failed_reclaim_continuation")
    parser.add_argument("--eval-horizons", default="120,240")
    parser.add_argument("--coverage-horizons", default="120,240")
    parser.add_argument("--stop-model", choices=("fixed_bps", "atr", "event_vwap"), default="event_vwap")
    parser.add_argument("--initial-stop-bps", type=float, default=30.0)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--atr-mult", type=float, default=1.0)
    parser.add_argument("--vwap-buffer-bps", type=float, default=15.0)
    parser.add_argument("--activation-r", type=float, default=2.0)
    parser.add_argument("--trail-bps", type=float, default=10.0)
    parser.add_argument("--wait", type=int, default=3)
    parser.add_argument("--max-entry-lag", type=int, default=2)
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    parser.add_argument("--folds", type=int, default=3)
    args = parser.parse_args()

    print("HyphyLiquid - Trailing Candidate Stability Report")
    print("=" * 72)

    cascades = [
        c for c in _load_cascades(CASCADES_PATH)
        if str(c.get("symbol", "")).upper() == args.symbol
        and c.get("side") == args.side
    ]
    candles_by_symbol = {args.symbol: _load_candles(args.symbol)}
    print(f"\nLoaded {len(cascades)} {args.symbol} side={args.side} cascades")
    print(f"  {args.symbol}: {len(candles_by_symbol[args.symbol])} 1m candles loaded")
    print(
        f"\nCandidate: variant={args.variant}, stop={args.stop_model}, "
        f"vwap_buffer={args.vwap_buffer_bps:g}bps, activation={args.activation_r:g}R, "
        f"trail={args.trail_bps:g}bps"
    )

    reports: list[dict] = []
    for coverage_horizon in _parse_csv_ints(args.coverage_horizons):
        entries = [
            t.to_dict() | {"lane": "btc_eth_trailing_resolution"}
            for t in run_backtest(
                cascades,
                candles_by_symbol,
                horizon_minutes=coverage_horizon,
                wait_minutes=args.wait,
                max_entry_lag_minutes=args.max_entry_lag,
            )
            if t.variant == args.variant
        ]
        for eval_horizon in _parse_csv_ints(args.eval_horizons):
            if eval_horizon > coverage_horizon:
                continue
            trades = apply_trailing_exits(
                entries,
                candles_by_symbol,
                initial_stop_bps=args.initial_stop_bps,
                activation_r=args.activation_r,
                trail_bps=args.trail_bps,
                max_hold_minutes=eval_horizon,
                round_trip_cost_bps=args.round_trip_cost_bps,
                stop_model=args.stop_model,
                atr_period=args.atr_period,
                atr_mult=args.atr_mult,
                vwap_buffer_bps=args.vwap_buffer_bps,
            )
            overall = summarize_trailing_rows(trades)
            reports.append({
                "slice": "overall",
                "coverage_horizon": coverage_horizon,
                "eval_horizon": eval_horizon,
                **overall,
            })
            for idx, fold in enumerate(_folds(trades, args.folds), start=1):
                reports.append({
                    "slice": f"fold_{idx}_of_{args.folds}",
                    "coverage_horizon": coverage_horizon,
                    "eval_horizon": eval_horizon,
                    **summarize_trailing_rows(fold),
                })

    out_path = OUT_DIR / f"trailing_stability_{args.symbol.lower()}_side_{args.side.lower()}.json"
    out_path.write_text(json.dumps(reports, indent=2), encoding="utf-8")

    print()
    print("=" * 106)
    print("Coverage / fold stability")
    print("=" * 106)
    print(
        f"  {'slice':<28} {'n':>4} {'WR%':>6} {'avg%':>8} {'med%':>8} "
        f"{'avgR':>7} {'PF':>7} {'act%':>6} {'initSL%':>8} {'trlSL%':>7}"
    )
    print("  " + "-" * 104)
    for row in reports:
        label = f"{row['eval_horizon']}m@{row['coverage_horizon']}m {row['slice']}"
        _print_row(label, row)

    print(f"\nWrote {len(reports)} rows to {out_path.name}")
    print("\nRead:")
    print("- Compare 120m@120m overall vs 120m@240m overall for coverage bias.")
    print("- Fold rows should not rely on one early/late pocket; any fold with n<10 is color only.")
    print("- This remains analysis-only until n>=100, median>0, and PF holds after costs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

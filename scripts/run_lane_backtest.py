"""Run lane-aware HyphyLiquid backtests.

This wrapper keeps v1 BTC/ETH research separate from alt research:
  - btc_eth_fade_or_follow: existing BTC/ETH fade/follow variants
  - alt_range_liq_scalp: research-only SOL/HYPE/DOGE/BNB range scalp
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_fade_or_follow_backtest import _load_candles, _load_cascades  # noqa: E402
from src.strategy.fade_or_follow_backtest import run_backtest, summarize  # noqa: E402
from src.strategy.lane_backtest import (  # noqa: E402
    ALT_RESEARCH_SYMBOLS,
    apply_r_multiple_exits,
    diagnostic_breakdown,
    run_alt_range_liq_scalp,
    summarize_exit_analysis,
    summarize_lane_trades,
)

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
OUT_DIR = PROJECT_ROOT / "data"
V1_SYMBOLS = {"BTC", "ETH"}


def _print_summary(summary: dict, lane: str) -> None:
    print()
    print("=" * 78)
    print(f"{lane} results")
    print("=" * 78)
    if not summary:
        print("  (no trades)")
        return
    print(
        f"  {'lane/variant':<30} {'sym':<6} {'n':>4} {'WR%':>6} "
        f"{'avg_net%':>9} {'med_net%':>9} {'PF':>7}  warning"
    )
    print("  " + "-" * 76)
    for row in sorted(summary.values(), key=lambda r: (r.get("lane") or r.get("variant"), r["symbol"])):
        name = row.get("lane") or row.get("variant")
        avg = row.get("avg_net_return_pct", row.get("avg_return_pct", 0.0))
        med = row.get("median_net_return_pct", row.get("median_return_pct", 0.0))
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        warning = "SAMPLE TOO SMALL" if row["n"] < 10 else ""
        print(
            f"  {name:<30} {row['symbol']:<6} {row['n']:>4} "
            f"{row['win_rate'] * 100:>5.1f}% {avg:>+8.4f} "
            f"{med:>+8.4f} {pf_str:>7}  {warning}"
        )


def _print_diagnostics(breakdown: dict) -> None:
    print()
    print("=" * 78)
    print("diagnostics")
    print("=" * 78)
    if not breakdown:
        print("  (no diagnostics)")
        return
    print(
        f"  {'bucket':<34} {'n':>4} {'WR%':>6} {'avg%':>9} "
        f"{'med%':>9} {'PF':>7} {'top_win_share':>14}"
    )
    print("  " + "-" * 84)
    for key, row in sorted(breakdown.items()):
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        print(
            f"  {key:<34} {row['n']:>4} {row['win_rate'] * 100:>5.1f}% "
            f"{row['avg_return_pct']:>+8.4f} {row['median_return_pct']:>+8.4f} "
            f"{pf_str:>7} {row['largest_win_share_of_gross_profit']:>14.2%}"
        )


def _print_exit_summary(summary: dict, lane: str) -> None:
    print()
    print("=" * 78)
    print(f"{lane} R-multiple exit results")
    print("=" * 78)
    if not summary:
        print("  (no trades)")
        return
    print(
        f"  {'lane/variant':<30} {'sym':<6} {'n':>4} {'WR%':>6} "
        f"{'avg_net%':>9} {'med_net%':>9} {'avgR':>7} {'avgSL':>7} {'PF':>7} "
        f"{'SL%':>6} {'TP%':>6} {'TO%':>6}"
    )
    print("  " + "-" * 92)
    for row in sorted(summary.values(), key=lambda r: (r["variant"], r["symbol"])):
        name = row.get("variant") or row.get("lane")
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        print(
            f"  {name:<30} {row['symbol']:<6} {row['n']:>4} "
            f"{row['win_rate'] * 100:>5.1f}% "
            f"{row['avg_net_return_pct']:>+8.4f} "
            f"{row['median_net_return_pct']:>+8.4f} "
            f"{row['avg_r']:>+6.2f} {row['avg_stop_bps']:>6.1f} {pf_str:>7} "
            f"{row['stop_rate'] * 100:>5.1f}% "
            f"{row['target_rate'] * 100:>5.1f}% "
            f"{row['timeout_rate'] * 100:>5.1f}%"
        )


def _load_candle_map(symbols: set[str]) -> dict[str, list[dict]]:
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in sorted(symbols):
        candles_by_symbol[sym] = _load_candles(sym)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles loaded")
    return candles_by_symbol


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane",
        choices=("btc_eth_fade_or_follow", "alt_range_liq_scalp"),
        default="alt_range_liq_scalp",
    )
    parser.add_argument("--symbol", choices=("BTC", "ETH", "SOL", "HYPE", "DOGE", "BNB"))
    parser.add_argument("--side", choices=("A", "B"),
                        help="Only include cascades from this side")
    parser.add_argument("--horizon", type=int, default=15)
    parser.add_argument("--wait", type=int, default=3)
    parser.add_argument("--max-entry-lag", type=int, default=2)
    parser.add_argument("--band-period", type=int, default=20)
    parser.add_argument("--stdev", type=float, default=2.0)
    parser.add_argument("--max-band-width-pct", type=float)
    parser.add_argument("--max-hold", type=int, default=15)
    parser.add_argument("--stop-buffer-bps", type=float, default=5.0)
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    parser.add_argument(
        "--exit-model",
        choices=("fixed_horizon", "r_multiple"),
        default="fixed_horizon",
        help="Keep fixed-horizon exits, or re-score entries with explicit stop/target",
    )
    parser.add_argument("--stop-bps", type=float, default=15.0,
                        help="Raw price stop distance in bps for --exit-model r_multiple")
    parser.add_argument("--target-r", type=float, default=2.5,
                        help="Take-profit multiple of stop distance for --exit-model r_multiple")
    parser.add_argument(
        "--stop-model",
        choices=("fixed_bps", "atr", "event_vwap"),
        default="fixed_bps",
        help="Stop model for --exit-model r_multiple",
    )
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--atr-mult", type=float, default=1.0)
    parser.add_argument("--vwap-buffer-bps", type=float, default=5.0)
    parser.add_argument("--diagnostics", action="store_true",
                        help="Print side/exit/regime breakdowns and outlier concentration")
    args = parser.parse_args()

    print("HyphyLiquid - Lane Backtest")
    print("=" * 60)

    cascades = _load_cascades(CASCADES_PATH)
    if args.lane == "btc_eth_fade_or_follow":
        allowed = V1_SYMBOLS
    else:
        allowed = ALT_RESEARCH_SYMBOLS
    if args.symbol:
        allowed = {args.symbol}
    cascades = [c for c in cascades if str(c.get("symbol", "")).upper() in allowed]
    if args.side:
        cascades = [c for c in cascades if c.get("side") == args.side]
    symbols = {str(c.get("symbol", "")).upper() for c in cascades if c.get("symbol")}
    filters = []
    if args.symbol:
        filters.append(f"symbol={args.symbol}")
    if args.side:
        filters.append(f"side={args.side}")
    filter_text = f" ({', '.join(filters)})" if filters else ""
    print(f"\nLoaded {len(cascades)} cascades for {args.lane}{filter_text}")
    candles_by_symbol = _load_candle_map(symbols)

    if args.lane == "btc_eth_fade_or_follow":
        trades = run_backtest(
            cascades,
            candles_by_symbol,
            horizon_minutes=args.horizon,
            wait_minutes=args.wait,
            max_entry_lag_minutes=args.max_entry_lag,
        )
        serializable = [t.to_dict() | {"lane": "btc_eth_fade_or_follow"} for t in trades]
        summary = summarize(trades)
        return_field = "return_pct"
    else:
        lane_trades = run_alt_range_liq_scalp(
            cascades,
            candles_by_symbol,
            band_period=args.band_period,
            stdev_mult=args.stdev,
            max_band_width_pct=args.max_band_width_pct,
            max_hold_minutes=args.max_hold,
            max_entry_lag_minutes=args.max_entry_lag,
            stop_buffer_bps=args.stop_buffer_bps,
            round_trip_cost_bps=args.round_trip_cost_bps,
        )
        serializable = [t.to_dict() for t in lane_trades]
        summary = summarize_lane_trades(lane_trades)
        return_field = "net_return_pct"

    if args.exit_model == "r_multiple":
        exit_trades = apply_r_multiple_exits(
            serializable,
            candles_by_symbol,
            stop_bps=args.stop_bps,
            target_r=args.target_r,
            max_hold_minutes=args.max_hold if args.lane == "alt_range_liq_scalp" else args.horizon,
            round_trip_cost_bps=args.round_trip_cost_bps,
            stop_model=args.stop_model,
            atr_period=args.atr_period,
            atr_mult=args.atr_mult,
            vwap_buffer_bps=args.vwap_buffer_bps,
        )
        serializable = [t.to_dict() for t in exit_trades]
        summary = summarize_exit_analysis(exit_trades)
        return_field = "net_return_pct"

    suffix = ""
    if args.symbol:
        suffix += f"_{args.symbol.lower()}"
    if args.side:
        suffix += f"_side_{args.side.lower()}"
    if args.exit_model == "r_multiple":
        if args.stop_model == "fixed_bps":
            suffix += f"_sl{args.stop_bps:g}bps_tp{args.target_r:g}r"
        elif args.stop_model == "atr":
            suffix += f"_atr{args.atr_mult:g}x_tp{args.target_r:g}r"
        else:
            suffix += f"_vwap{args.vwap_buffer_bps:g}bps_tp{args.target_r:g}r"
    out_path = OUT_DIR / f"lane_backtest_{args.lane}{suffix}_trades.jsonl"
    out_path.write_text(
        "\n".join(json.dumps(row) for row in serializable) + ("\n" if serializable else ""),
        encoding="utf-8",
    )
    print(f"\nWrote {len(serializable)} trades to {out_path.name}")
    if args.exit_model == "r_multiple":
        _print_exit_summary(summary, args.lane)
    else:
        _print_summary(summary, args.lane)
    if args.diagnostics:
        _print_diagnostics(
            diagnostic_breakdown(
                serializable,
                return_field=return_field,
                include_band_buckets=args.lane == "alt_range_liq_scalp",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

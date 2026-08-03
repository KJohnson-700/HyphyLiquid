"""Sweep raw-price stop distances and R-multiple targets for lane entries.

This is an analysis wrapper only. It does not change paper or live execution.
Stops are raw price basis points, not ROE. Targets are multiples of that stop.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_fade_or_follow_backtest import _load_candles, _load_cascades  # noqa: E402
from src.strategy.fade_or_follow_backtest import run_backtest  # noqa: E402
from src.strategy.lane_backtest import (  # noqa: E402
    ALT_RESEARCH_SYMBOLS,
    apply_r_multiple_exits,
    run_alt_range_liq_scalp,
    summarize_exit_analysis,
)

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
OUT_DIR = PROJECT_ROOT / "data"
V1_SYMBOLS = {"BTC", "ETH"}


def _parse_csv_floats(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _pf_value(raw: float | str) -> float:
    return float("inf") if raw == "inf" else float(raw)


def _load_candle_map(symbols: set[str]) -> dict[str, list[dict]]:
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in sorted(symbols):
        candles_by_symbol[sym] = _load_candles(sym)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles loaded")
    return candles_by_symbol


def _entry_trades(args: argparse.Namespace, cascades: list[dict], candles_by_symbol: dict[str, list[dict]]) -> list[dict]:
    if args.lane == "btc_eth_fade_or_follow":
        trades = run_backtest(
            cascades,
            candles_by_symbol,
            horizon_minutes=args.horizon,
            wait_minutes=args.wait,
            max_entry_lag_minutes=args.max_entry_lag,
        )
        return [t.to_dict() | {"lane": args.lane} for t in trades]

    trades = run_alt_range_liq_scalp(
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
    return [t.to_dict() for t in trades]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=("btc_eth_fade_or_follow", "alt_range_liq_scalp"),
                        default="btc_eth_fade_or_follow")
    parser.add_argument("--symbol", choices=("BTC", "ETH", "SOL", "HYPE", "DOGE", "BNB"))
    parser.add_argument("--side", choices=("A", "B"))
    parser.add_argument("--stops-bps", default="10,15,20,30",
                        help="Comma-separated raw-price stop distances in bps")
    parser.add_argument("--targets-r", default="1,1.5,2,2.5",
                        help="Comma-separated take-profit multiples")
    parser.add_argument(
        "--stop-models",
        default="fixed_bps,atr,event_vwap",
        help="Comma-separated stop models: fixed_bps, atr, event_vwap",
    )
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--atr-mults", default="0.5,0.75,1,1.5",
                        help="Comma-separated ATR multiples for stop_model=atr")
    parser.add_argument("--vwap-buffers-bps", default="0,5,10,15",
                        help="Comma-separated VWAP buffers for stop_model=event_vwap")
    parser.add_argument("--horizon", type=int, default=15)
    parser.add_argument("--wait", type=int, default=3)
    parser.add_argument("--max-entry-lag", type=int, default=2)
    parser.add_argument("--band-period", type=int, default=20)
    parser.add_argument("--stdev", type=float, default=2.0)
    parser.add_argument("--max-band-width-pct", type=float)
    parser.add_argument("--max-hold", type=int, default=15)
    parser.add_argument("--stop-buffer-bps", type=float, default=5.0)
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    args = parser.parse_args()

    print("HyphyLiquid - TP/SL Sweep")
    print("=" * 60)

    allowed = V1_SYMBOLS if args.lane == "btc_eth_fade_or_follow" else ALT_RESEARCH_SYMBOLS
    if args.symbol:
        allowed = {args.symbol}
    cascades = [
        c for c in _load_cascades(CASCADES_PATH)
        if str(c.get("symbol", "")).upper() in allowed
        and (not args.side or c.get("side") == args.side)
    ]
    symbols = {str(c.get("symbol", "")).upper() for c in cascades if c.get("symbol")}
    filters = []
    if args.symbol:
        filters.append(f"symbol={args.symbol}")
    if args.side:
        filters.append(f"side={args.side}")
    filter_text = f" ({', '.join(filters)})" if filters else ""
    print(f"\nLoaded {len(cascades)} cascades for {args.lane}{filter_text}")
    candles_by_symbol = _load_candle_map(symbols)
    entries = _entry_trades(args, cascades, candles_by_symbol)
    print(f"\nCandidate entries: {len(entries)}")

    rows: list[dict] = []
    max_hold = args.max_hold if args.lane == "alt_range_liq_scalp" else args.horizon
    stop_models = [m.strip() for m in args.stop_models.split(",") if m.strip()]
    configs: list[dict] = []
    for model in stop_models:
        if model == "fixed_bps":
            configs.extend({"stop_model": model, "stop_bps": v} for v in _parse_csv_floats(args.stops_bps))
        elif model == "atr":
            configs.extend({"stop_model": model, "atr_mult": v} for v in _parse_csv_floats(args.atr_mults))
        elif model == "event_vwap":
            configs.extend({"stop_model": model, "vwap_buffer_bps": v} for v in _parse_csv_floats(args.vwap_buffers_bps))
        else:
            raise ValueError(f"unsupported stop model: {model}")

    for config in configs:
        for target_r in _parse_csv_floats(args.targets_r):
            rescored = apply_r_multiple_exits(
                entries,
                candles_by_symbol,
                stop_bps=float(config.get("stop_bps", 0.0)),
                target_r=target_r,
                max_hold_minutes=max_hold,
                round_trip_cost_bps=args.round_trip_cost_bps,
                stop_model=str(config["stop_model"]),
                atr_period=args.atr_period,
                atr_mult=float(config.get("atr_mult", 1.0)),
                vwap_buffer_bps=float(config.get("vwap_buffer_bps", 5.0)),
            )
            for row in summarize_exit_analysis(rescored).values():
                rows.append({
                    "lane": row["lane"],
                    "variant": row["variant"],
                    "symbol": row["symbol"],
                    "stop_model": config["stop_model"],
                    "config_stop_bps": config.get("stop_bps"),
                    "atr_mult": config.get("atr_mult"),
                    "vwap_buffer_bps": config.get("vwap_buffer_bps"),
                    "avg_effective_stop_bps": row["avg_stop_bps"],
                    "target_r": target_r,
                    **row,
                })

    suffix = ""
    if args.symbol:
        suffix += f"_{args.symbol.lower()}"
    if args.side:
        suffix += f"_side_{args.side.lower()}"
    out_path = OUT_DIR / f"tp_sl_sweep_{args.lane}{suffix}.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print()
    print("=" * 100)
    print("TP/SL sweep results")
    print("=" * 100)
    print(
        f"  {'variant':<30} {'sym':<6} {'model':<10} {'cfg':>7} {'TPR':>5} "
        f"{'n':>4} {'WR%':>6} {'avg%':>8} {'med%':>8} {'avgR':>7} "
        f"{'PF':>7} {'SL%':>6} {'TP%':>6} {'TO%':>6}"
    )
    print("  " + "-" * 116)
    for row in sorted(rows, key=lambda r: (-_pf_value(r["profit_factor"]), -r["n"], r["variant"], r["stop_model"], r["target_r"])):
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        if row["stop_model"] == "fixed_bps":
            cfg = f"{row['config_stop_bps']:g}bps"
        elif row["stop_model"] == "atr":
            cfg = f"{row['atr_mult']:g}x"
        else:
            cfg = f"{row['vwap_buffer_bps']:g}bps"
        print(
            f"  {row['variant']:<30} {row['symbol']:<6} {row['stop_model']:<10} "
            f"{cfg:>7} {row['target_r']:>5.1f} {row['n']:>4} {row['win_rate'] * 100:>5.1f}% "
            f"{row['avg_net_return_pct']:>+7.4f} {row['median_net_return_pct']:>+7.4f} "
            f"{row['avg_r']:>+6.2f} {pf_str:>7} {row['stop_rate'] * 100:>5.1f}% "
            f"{row['target_rate'] * 100:>5.1f}% {row['timeout_rate'] * 100:>5.1f}%"
        )
    print(f"\nWrote {len(rows)} summary rows to {out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

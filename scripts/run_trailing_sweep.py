"""Sweep longer-horizon trailing exits for BTC/ETH cascade entries.

This is analysis-only. It keeps the existing cascade entry variants, then
tests whether longer holds plus trailing stops fit BTC/ETH better than scalp
TP/SL exits.
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
from src.strategy.lane_backtest import apply_trailing_exits, summarize_trailing_analysis  # noqa: E402

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
OUT_DIR = PROJECT_ROOT / "data"
V1_SYMBOLS = {"BTC", "ETH"}


def _parse_csv_floats(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_csv_ints(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _pf_value(raw: float | str) -> float:
    return float("inf") if raw == "inf" else float(raw)


def _load_candle_map(symbols: set[str]) -> dict[str, list[dict]]:
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in sorted(symbols):
        candles_by_symbol[sym] = _load_candles(sym)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles loaded")
    return candles_by_symbol


def _config_label(row: dict) -> str:
    if row["stop_model"] == "fixed_bps":
        return f"{row['config_initial_stop_bps']:g}bps"
    if row["stop_model"] == "atr":
        return f"{row['atr_mult']:g}x"
    return f"{row['vwap_buffer_bps']:g}bps"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("BTC", "ETH"))
    parser.add_argument("--side", choices=("A", "B"))
    parser.add_argument("--variants", default="baseline_fade,reclaim_fade,failed_reclaim_continuation",
                        help="Comma-separated entry variants to include")
    parser.add_argument("--horizons", default="30,60,120,240",
                        help="Comma-separated max holds in minutes")
    parser.add_argument("--stop-models", default="fixed_bps,atr,event_vwap")
    parser.add_argument("--initial-stops-bps", default="15,20,30,50",
                        help="Fixed initial stop distances for stop_model=fixed_bps")
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--atr-mults", default="1,1.5,2")
    parser.add_argument("--vwap-buffers-bps", default="5,10,15,25")
    parser.add_argument("--activation-rs", default="0.75,1,1.5",
                        help="R multiple required before trailing activates")
    parser.add_argument("--trail-bps", default="10,15,20,30",
                        help="Raw-price trailing distance in bps")
    parser.add_argument("--wait", type=int, default=3)
    parser.add_argument("--max-entry-lag", type=int, default=2)
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    parser.add_argument("--top", type=int, default=40,
                        help="Number of ranked rows to print; JSON output still includes all rows")
    args = parser.parse_args()

    print("HyphyLiquid - BTC/ETH Trailing Resolution Sweep")
    print("=" * 60)

    allowed = {args.symbol} if args.symbol else V1_SYMBOLS
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
    print(f"\nLoaded {len(cascades)} cascades{filter_text}")
    candles_by_symbol = _load_candle_map(symbols)

    max_horizon = max(_parse_csv_ints(args.horizons))
    entries = [
        t.to_dict() | {"lane": "btc_eth_trailing_resolution"}
        for t in run_backtest(
            cascades,
            candles_by_symbol,
            horizon_minutes=max_horizon,
            wait_minutes=args.wait,
            max_entry_lag_minutes=args.max_entry_lag,
        )
    ]
    variants = {v.strip() for v in args.variants.split(",") if v.strip()}
    entries = [e for e in entries if e.get("variant") in variants]
    print(f"\nCandidate entries with {max_horizon}m coverage: {len(entries)}")

    configs: list[dict] = []
    for model in [m.strip() for m in args.stop_models.split(",") if m.strip()]:
        if model == "fixed_bps":
            configs.extend({"stop_model": model, "initial_stop_bps": v} for v in _parse_csv_floats(args.initial_stops_bps))
        elif model == "atr":
            configs.extend({"stop_model": model, "atr_mult": v} for v in _parse_csv_floats(args.atr_mults))
        elif model == "event_vwap":
            configs.extend({"stop_model": model, "vwap_buffer_bps": v} for v in _parse_csv_floats(args.vwap_buffers_bps))
        else:
            raise ValueError(f"unsupported stop model: {model}")

    rows: list[dict] = []
    for horizon in _parse_csv_ints(args.horizons):
        for config in configs:
            for activation_r in _parse_csv_floats(args.activation_rs):
                for trail_bps in _parse_csv_floats(args.trail_bps):
                    trades = apply_trailing_exits(
                        entries,
                        candles_by_symbol,
                        initial_stop_bps=float(config.get("initial_stop_bps", 0.0)),
                        activation_r=activation_r,
                        trail_bps=trail_bps,
                        max_hold_minutes=horizon,
                        round_trip_cost_bps=args.round_trip_cost_bps,
                        stop_model=str(config["stop_model"]),
                        atr_period=args.atr_period,
                        atr_mult=float(config.get("atr_mult", 1.0)),
                        vwap_buffer_bps=float(config.get("vwap_buffer_bps", 5.0)),
                    )
                    for row in summarize_trailing_analysis(trades).values():
                        rows.append({
                            "lane": row["lane"],
                            "variant": row["variant"],
                            "symbol": row["symbol"],
                            "horizon": horizon,
                            "stop_model": config["stop_model"],
                            "config_initial_stop_bps": config.get("initial_stop_bps"),
                            "atr_mult": config.get("atr_mult"),
                            "vwap_buffer_bps": config.get("vwap_buffer_bps"),
                            "activation_r": activation_r,
                            "trail_bps": trail_bps,
                            **row,
                        })

    suffix = ""
    if args.symbol:
        suffix += f"_{args.symbol.lower()}"
    if args.side:
        suffix += f"_side_{args.side.lower()}"
    out_path = OUT_DIR / f"trailing_sweep_btc_eth{suffix}.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print()
    print("=" * 132)
    print("Trailing resolution results")
    print("=" * 132)
    print(
        f"  {'variant':<30} {'sym':<5} {'hold':>4} {'model':<10} {'cfg':>7} "
        f"{'actR':>5} {'trail':>6} {'n':>4} {'WR%':>6} {'avg%':>8} "
        f"{'med%':>8} {'avgR':>7} {'PF':>7} {'act%':>6} {'initSL%':>8} {'trlSL%':>7}"
    )
    print("  " + "-" * 130)
    ranked = sorted(rows, key=lambda r: (-_pf_value(r["profit_factor"]), -r["n"], r["variant"], r["horizon"]))
    for row in ranked[:args.top]:
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        print(
            f"  {row['variant']:<30} {row['symbol']:<5} {row['horizon']:>4} "
            f"{row['stop_model']:<10} {_config_label(row):>7} {row['activation_r']:>5.2f} "
            f"{row['trail_bps']:>5.1f}b {row['n']:>4} {row['win_rate'] * 100:>5.1f}% "
            f"{row['avg_net_return_pct']:>+7.4f} {row['median_net_return_pct']:>+7.4f} "
            f"{row['avg_r']:>+6.2f} {pf_str:>7} {row['activation_rate'] * 100:>5.1f}% "
            f"{row['initial_stop_rate'] * 100:>7.1f}% {row['trailing_stop_rate'] * 100:>6.1f}%"
        )
    if len(ranked) > args.top:
        print(f"  ... {len(ranked) - args.top} more rows in JSON")
    print(f"\nWrote {len(rows)} summary rows to {out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

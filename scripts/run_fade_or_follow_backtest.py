"""
HyphyLiquid - run the fade_or_follow backtest on real data and
print the per-(variant, symbol) report.

Reads:
  data/cascades.jsonl         - canonical cascade events with features
  data/ws_candle/{sym}_*.jsonl - 1m live candles per symbol

Writes:
  data/backtest_trades.jsonl   - per-trade records
  (no report file, prints to stdout)

This is the spec build order Task 3 + Task 4: build the first
feature-backed backtest and add a simple results report.

Variants compared:
  baseline_fade
  reclaim_fade
  failed_reclaim_continuation
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.cascade_cluster import cluster_events
from src.strategy.fade_or_follow_backtest import (
    Trade,
    run_backtest,
    summarize,
)

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
TRADES_PATH = PROJECT_ROOT / "data" / "backtest_trades.jsonl"
LIQ_PATH = PROJECT_ROOT / "data" / "liquidations.jsonl"


def _load_cascades(path: Path) -> list[dict]:
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


def _load_candles(symbol: str, date_str: str) -> list[dict]:
    """Load all 1m candle records for symbol on date, return list of
    dicts with at least 't' (ms) and 'c' (close)."""
    path = PROJECT_ROOT / "data" / "ws_candle" / f"{symbol.lower()}_{date_str}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else None
        if not isinstance(payload, dict):
            continue
        if payload.get("s", "").upper() != symbol.upper():
            continue
        t = payload.get("t")
        c = payload.get("c")
        if t is None or c is None:
            continue
        try:
            out.append({"t": int(t), "c": float(c), "o": float(payload.get("o", 0)),
                        "h": float(payload.get("h", 0)), "l": float(payload.get("l", 0)),
                        "v": float(payload.get("v", 0)), "n": int(payload.get("n", 0))})
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda b: b["t"])
    return out


def _print_report(summary: dict, total_cascades: int, n_cascades_evaluated: int,
                  n_skipped_no_candles: int, n_skipped_no_exit: int) -> None:
    print()
    print("=" * 78)
    print("FADE_OR_FOLLOW BACKTEST — first results on live data")
    print("=" * 78)
    print(f"  Total cascades in file:        {total_cascades}")
    print(f"  Cascades with candle coverage:  {n_cascades_evaluated}")
    print(f"  Skipped (no candles for sym):   {n_skipped_no_candles}")
    print(f"  Skipped (no exit bar):          {n_skipped_no_exit}")
    print()
    # Group by variant for the headline view
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for k, v in summary.items():
        by_variant[v["variant"]].append(v)
    print(f"  {'variant':<30} {'sym':<6} {'n':>4} {'WR%':>6} {'avg_pnl%':>9} "
          f"{'med_pnl%':>9} {'PF':>7}  {'warning'}")
    print("  " + "-" * 76)
    for variant in ("baseline_fade", "reclaim_fade", "failed_reclaim_continuation"):
        rows = by_variant.get(variant, [])
        if not rows:
            print(f"  {variant:<30} (no trades)")
            continue
        for r in sorted(rows, key=lambda x: x["symbol"]):
            warning = ""
            if r["n"] < 10:
                warning = "SAMPLE TOO SMALL"
            pf = r["profit_factor"]
            pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
            print(f"  {variant:<30} {r['symbol']:<6} {r['n']:>4} "
                  f"{r['win_rate']*100:>5.1f}% {r['avg_return_pct']:>+8.4f} "
                  f"{r['median_return_pct']:>+8.4f} {pf_str:>7}  {warning}")
        # Per-variant aggregate
        all_n = sum(r["n"] for r in rows)
        if all_n > 0:
            all_rets: list[float] = []
            # Cannot easily recompute from per-symbol summaries, skip the
            # all-symbol aggregate; per-symbol is the actionable level.

    print()
    print("INTERPRETATION GUIDE")
    print("-" * 78)
    print("  baseline_fade: control - the original 'always fade' rule.")
    print("  reclaim_fade: only trades when price reclaims event VWAP within wait.")
    print("  failed_reclaim_continuation: only trades when NO reclaim + holds.")
    print("  If reclaim_fade WR > baseline_fade WR AND avg > baseline avg,")
    print("  the response filter adds edge. If they're similar, no value yet.")
    print("  If failed_reclaim_continuation > both, fades are wrong direction,")
    print("  and following cascades is the actual edge.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", type=int, default=15, help="Hold minutes (default 15)")
    parser.add_argument("--wait", type=int, default=3, help="Wait minutes for reclaim (default 3)")
    parser.add_argument("--symbol", choices=("BTC", "ETH", "SOL", "HYPE"),
                        help="Only backtest this symbol")
    parser.add_argument("--rebuild-cascades", action="store_true",
                        help="Rebuild cascades.jsonl from liquidations.jsonl first")
    args = parser.parse_args()

    print("HyphyLiquid - Fade_or_Follow Backtest")
    print("=" * 60)

    if args.rebuild_cascades:
        # Defer to build_cascades script logic (avoid re-implementing)
        import subprocess
        subprocess.run([sys.executable, "scripts/build_cascades.py"], check=True)

    cascades = _load_cascades(CASCADES_PATH)
    print(f"\nLoaded {len(cascades)} cascades from {CASCADES_PATH.name}")
    if args.symbol:
        cascades = [c for c in cascades if c.get("symbol") == args.symbol]
        print(f"  filtered to {len(cascades)} {args.symbol} cascades")

    # Determine the date of candle data we have
    # 1m candles are written to {sym}_YYYY-MM-DD.jsonl
    # For our 2026-08-02 runs, the candle data is in data/ws_candle/
    candle_date = "2026-08-02"

    # Load candles per symbol
    symbols = sorted({c.get("symbol") for c in cascades if c.get("symbol")})
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        candles_by_symbol[sym] = _load_candles(sym, candle_date)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles loaded")

    # Pre-filter cascades
    total = len(cascades)
    n_skipped_no_candles = sum(1 for c in cascades if not candles_by_symbol.get(c.get("symbol")))
    n_evaluated_pre = sum(1 for c in cascades if candles_by_symbol.get(c.get("symbol")))
    trades = run_backtest(
        cascades,
        candles_by_symbol,
        horizon_minutes=args.horizon,
        wait_minutes=args.wait,
    )
    n_skipped_no_exit = n_evaluated_pre - len({t.cascade_start_ts for t in trades})

    # Write trades
    TRADES_PATH.write_text(
        "\n".join(json.dumps(t.to_dict()) for t in trades) + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {len(trades)} trades to {TRADES_PATH.name}")

    # Summarize
    summary = summarize(trades)
    _print_report(summary, total, n_evaluated_pre, n_skipped_no_candles, n_skipped_no_exit)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

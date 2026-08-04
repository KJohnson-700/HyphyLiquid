"""Run feature-bucket diagnostics for BTC/ETH cascade lane trades.

Reads the latest enriched cascades and the latest BTC/ETH lane backtest
trades, joins them by cascade timestamp/symbol/side, and writes a ranked
JSON report. Analysis-only; no execution path imports this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.filter_diagnostics import (  # noqa: E402
    diagnostic_groups,
    enrich_trades_with_filters,
)

CASCADES_PATH = PROJECT_ROOT / "data" / "cascades.jsonl"
BTC_ETH_TRADES_PATH = PROJECT_ROOT / "data" / "lane_backtest_btc_eth_fade_or_follow_trades.jsonl"
OUT_PATH = PROJECT_ROOT / "data" / "btc_eth_filter_diagnostics.json"
V1_SYMBOLS = {"BTC", "ETH"}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _print_rows(rows: list[dict], limit: int) -> None:
    print()
    print("=" * 120)
    print("BTC/ETH filter diagnostics")
    print("=" * 120)
    print(
        f"  {'group':<34} {'bucket':<52} {'n':>4} {'WR%':>6} "
        f"{'avg%':>8} {'med%':>8} {'PF':>7} {'top_win':>8}"
    )
    print("  " + "-" * 118)
    for row in rows[:limit]:
        pf = row["profit_factor"]
        pf_str = f"{pf:>6.2f}" if isinstance(pf, (int, float)) else f"{pf:>7}"
        print(
            f"  {row['group']:<34} {row['bucket']:<52} {row['n']:>4} "
            f"{row['win_rate'] * 100:>5.1f}% {row['avg_return_pct']:>+7.4f} "
            f"{row['median_return_pct']:>+7.4f} {pf_str:>7} "
            f"{row['top_win_share']:>7.1%}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cascades", default=str(CASCADES_PATH))
    parser.add_argument("--trades", default=str(BTC_ETH_TRADES_PATH))
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--min-n", type=int, default=20)
    parser.add_argument("--top", type=int, default=40)
    args = parser.parse_args()

    cascades_path = Path(args.cascades)
    trades_path = Path(args.trades)
    out_path = Path(args.out)
    if not cascades_path.is_absolute():
        cascades_path = (PROJECT_ROOT / cascades_path).resolve()
    if not trades_path.is_absolute():
        trades_path = (PROJECT_ROOT / trades_path).resolve()
    if not out_path.is_absolute():
        out_path = (PROJECT_ROOT / out_path).resolve()

    cascades = [c for c in _load_jsonl(cascades_path) if str(c.get("symbol", "")).upper() in V1_SYMBOLS]
    trades = [t for t in _load_jsonl(trades_path) if str(t.get("symbol", "")).upper() in V1_SYMBOLS]
    enriched = enrich_trades_with_filters(trades, cascades)
    rows = diagnostic_groups(enriched, return_field="return_pct", min_n=args.min_n)

    payload = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "source_cascades": str(cascades_path.relative_to(PROJECT_ROOT)),
        "source_trades": str(trades_path.relative_to(PROJECT_ROOT)),
        "v1_symbols": sorted(V1_SYMBOLS),
        "cascade_rows": len(cascades),
        "trade_rows": len(trades),
        "joined_rows": len(enriched),
        "min_n": args.min_n,
        "diagnostics": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("HyphyLiquid - BTC/ETH Filter Diagnostics")
    print("=" * 60)
    print(f"Loaded cascades: {len(cascades)}")
    print(f"Loaded trades:   {len(trades)}")
    print(f"Joined rows:     {len(enriched)}")
    print(f"Report rows:     {len(rows)}")
    _print_rows(rows, args.top)
    print(f"\nWrote {len(rows)} rows to {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

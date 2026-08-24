"""Check that the panels the strategy reads actually cover enough history.

The failure this exists to catch: every daemon step reports "ok" while the
funding panel quietly covers only the last day, because the jsonl builders
read files that disk maintenance already deleted. The strategy then finds no
signals and the closed-trade count sits still, which looks identical to
"the market gave us nothing".

A funding panel much shorter than the candle panel is the tell -- the two are
inner-joined before the signal runs, so the join collapses to the shorter one.

Usage:
  python3 scripts/panel_health.py
  python3 scripts/panel_health.py --min-hours 100
Exit code 1 if any traded symbol fails, so it can gate a daemon or a cron.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANDLE = PROJECT_ROOT / "data" / "candle_panel.csv"
FUNDING = PROJECT_ROOT / "data" / "funding_panel.csv"

# the symbols with a calibrated PER_ASSET_POLICY -- the only ones that trade
TRADED = ["BTC", "ETH", "HYPE", "SOL"]


def _cov(path: Path) -> dict:
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return {
        s: (len(d), d["ts"].min(), d["ts"].max())
        for s, d in df.groupby("symbol")
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hours", type=int, default=100,
                    help="hourly rows a traded symbol needs in BOTH panels")
    ap.add_argument("--max-gap", type=int, default=24,
                    help="hours the funding panel may lag the candle panel by")
    args = ap.parse_args()

    candle, funding = _cov(CANDLE), _cov(FUNDING)
    if not candle or not funding:
        print("FAIL: a panel is missing entirely", file=sys.stderr)
        return 1

    bad = []
    print(f"{'symbol':8} {'candle':>7} {'funding':>8}  {'joined range':>24}")
    for sym in TRADED:
        c, f = candle.get(sym), funding.get(sym)
        if not c or not f:
            print(f"{sym:8} {'--':>7} {'--':>8}  MISSING FROM A PANEL")
            bad.append(sym)
            continue
        # the strategy sees the intersection, not either panel alone
        lo, hi = max(c[1], f[1]), min(c[2], f[2])
        joined = int((hi - lo).total_seconds() // 3600) + 1 if hi >= lo else 0
        flag = ""
        if joined < args.min_hours:
            flag = f"  <-- joined {joined}h < {args.min_hours}h"
            bad.append(sym)
        elif (c[2] - f[2]).total_seconds() / 3600 > args.max_gap:
            flag = "  <-- funding panel is stale vs candles"
            bad.append(sym)
        print(f"{sym:8} {c[0]:>7} {f[0]:>8}  {joined:>5}h {lo:%m-%d %H} -> {hi:%m-%d %H}{flag}")

    if bad:
        print(f"\nFAIL: {', '.join(bad)}", file=sys.stderr)
        print("Backfill from DuckDB:  python3 scripts/build_panels_from_duckdb.py --merge",
              file=sys.stderr)
        return 1
    print("\nok: every traded symbol has enough joined history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

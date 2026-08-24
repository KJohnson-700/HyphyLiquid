"""Build candle_panel.csv and funding_panel.csv from data/hyphyliquid.duckdb.

The jsonl-based builders (build_candle_panel.py / build_funding_panel.py) only
see what the local WS daemons have captured — about 12 hours on a fresh box.
hyphyliquid.duckdb carries the full history (ws_candle from 2026-08-16,
asset_ctx from 2026-08-17, 17 symbols), which is what the backtests and the
graduation scorecard actually need.

Emits the exact same CSV schemas the jsonl builders do, so every downstream
reader (strategy_search.load_hl_with_funding and friends) works unchanged:

  candle_panel.csv   ts,symbol,open,high,low,close,volume
  funding_panel.csv  ts,symbol,funding_actual,funding_predicted,markPx,openInterest

ws_candle rows are 1-minute bars; they are rolled up to hourly here.
asset_ctx is polled ~every 60s; the last poll in each hour wins, matching how
a bar-close strategy would observe funding.

Usage:
  python3 scripts/build_panels_from_duckdb.py
  python3 scripts/build_panels_from_duckdb.py --db path.duckdb --min-bars 100
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB = PROJECT_ROOT / "data" / "hyphyliquid.duckdb"
CANDLE_OUT = PROJECT_ROOT / "data" / "candle_panel.csv"
FUNDING_OUT = PROJECT_ROOT / "data" / "funding_panel.csv"

CANDLE_SQL = """
COPY (
  WITH hourly AS (
    SELECT
      date_trunc('hour', to_timestamp(t_open / 1000)) AS ts,
      symbol,
      arg_min(open,  t_open) AS open,
      max(high)              AS high,
      min(low)               AS low,
      arg_max(close, t_open) AS close,
      sum(volume)            AS volume
    FROM ws_candle
    WHERE symbol IS NOT NULL AND t_open IS NOT NULL
    GROUP BY 1, 2
  )
  SELECT ts, symbol, open, high, low, close, volume
  FROM hourly ORDER BY symbol, ts
) TO '{out}' (HEADER, DELIMITER ',');
"""

# last poll of each hour wins — that is what a bar-close strategy would see
FUNDING_SQL = """
COPY (
  WITH ranked AS (
    SELECT
      date_trunc('hour', poll_ts) AS ts,
      symbol,
      TRY_CAST(funding AS DOUBLE)        AS funding_actual,
      TRY_CAST(predicted_fund AS DOUBLE) AS funding_predicted,
      TRY_CAST(mark_px AS DOUBLE)        AS markPx,
      TRY_CAST(open_interest AS DOUBLE)  AS openInterest,
      row_number() OVER (
        PARTITION BY symbol, date_trunc('hour', poll_ts) ORDER BY poll_ts DESC
      ) AS rn
    FROM asset_ctx
    WHERE symbol IS NOT NULL AND poll_ts IS NOT NULL
  )
  SELECT ts, symbol, funding_actual, funding_predicted, markPx, openInterest
  FROM ranked WHERE rn = 1 ORDER BY symbol, ts
) TO '{out}' (HEADER, DELIMITER ',');
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--min-bars", type=int, default=100,
                    help="warn for symbols under this many hourly bars")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: {args.db} not found", file=sys.stderr)
        return 1

    import duckdb  # local import so the script fails loudly if the dep is absent

    con = duckdb.connect(str(args.db), read_only=True)
    con.execute(CANDLE_SQL.format(out=CANDLE_OUT))
    con.execute(FUNDING_SQL.format(out=FUNDING_OUT))

    cov = con.execute("""
        SELECT symbol,
               count(DISTINCT date_trunc('hour', to_timestamp(t_open/1000))) AS bars,
               min(to_timestamp(t_open/1000))::date AS lo,
               max(to_timestamp(t_open/1000))::date AS hi
        FROM ws_candle WHERE symbol IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    con.close()

    print(f"wrote {CANDLE_OUT}")
    print(f"wrote {FUNDING_OUT}")
    print(f"\nhourly bar coverage (min-bars={args.min_bars}):")
    for symbol, bars, lo, hi in cov:
        flag = "ok " if bars >= args.min_bars else "LOW"
        print(f"  [{flag}] {symbol:14} {bars:5} bars   {lo} -> {hi}")
    low = [s for s, b, *_ in cov if b < args.min_bars]
    if low:
        print(f"\n  below {args.min_bars} bars, stay research-only: {', '.join(low)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

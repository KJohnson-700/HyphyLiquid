"""
HyphyLiquid - run the liquidation backtest on the live liquidation data.

Loads detected events from data/liquidations.jsonl, runs the fade
backtest at multiple horizons, prints honest metrics.
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.strategy.liquidation_backtest import run_liquidation_backtest

DATA_DIR = PROJECT_ROOT / "data"


def _load_liquidations() -> list[dict]:
    path = DATA_DIR / "liquidations.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").strip().splitlines() if l]


def _load_candles(symbol: str) -> pd.DataFrame:
    """Load candles. Prefer freshest lookback so live events have data."""
    candidates: list[Path] = []
    for env in ("mainnet", "testnet"):
        for suffix in ("7d", "30d", "90d"):
            candidates.append(DATA_DIR / f"{symbol.lower()}_candles_1h_{suffix}_{env}.csv")
    for path in candidates:
        if path.exists():
            c = pd.read_csv(path)
            c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
            return c
    return pd.DataFrame()


def main() -> int:
    print("HyphyLiquid - Liquidation Backtest")
    print("=" * 60)

    events = _load_liquidations()
    print(f"\nLoaded {len(events)} detected liquidation events")
    if not events:
        print("No events yet. Let the pipeline run and try again later.")
        return 0

    # Group by symbol
    by_symbol: dict[str, pd.DataFrame] = {}
    for sym in sorted({e["symbol"] for e in events}):
        c = _load_candles(sym)
        if c.empty:
            print(f"  WARNING: no candle data for {sym}")
            continue
        by_symbol[sym] = c
        print(f"  {sym}: {len(c)} candles ({c['timestamp'].min()} -> {c['timestamp'].max()})")

    if not by_symbol:
        print("No candle data, aborting.")
        return 1

    # Split events by whether they fall within candle window.
    # The last candle's timestamp is the OPEN time, so events within the
    # still-forming last candle have ts > c.max(). We treat anything up to
    # c.max() + 1h as "in window" (within the open candle or completed ones).
    in_window = []
    out_window = 0
    for e in events:
        e_ts = pd.Timestamp(e["ts"])
        sym = e["symbol"]
        if sym not in by_symbol:
            continue
        c = by_symbol[sym]
        window_end = c["timestamp"].max() + pd.Timedelta(hours=1)
        if c["timestamp"].min() <= e_ts <= window_end:
            in_window.append(e)
        else:
            out_window += 1
    print(f"\nEvents in candle window: {len(in_window)}  (out of window: {out_window})")

    if not in_window:
        print("No events overlap with candle window. Let the pipeline accumulate "
              "and re-fetch candles before running again.")
        return 0

    # Run backtest
    results = run_liquidation_backtest(
        liquidation_events=in_window,
        candles_by_symbol=by_symbol,
        entry_window=1,
        exit_horizons=(1, 4, 24, 72),
    )

    print("\n" + "=" * 60)
    print("RESULTS (fade direction, slippage 5bps, 0.045% fees)")
    print("=" * 60)
    print(f"  {'horizon':>8}  {'n':>4}  {'WR%':>6}  {'avg_pnl%':>10}  {'med_pnl%':>10}  {'PF':>7}  {'avg_bars':>9}")
    for h, r in sorted(results.items()):
        if r.total == 0:
            continue
        pf_str = f"{r.profit_factor:>6.2f}" if r.profit_factor != float("inf") else "    inf"
        print(
            f"  {h:>7}h  {r.total:>4}  {r.win_rate*100:>5.1f}  {r.avg_pnl_pct*100:>+9.2f}  "
            f"{r.median_pnl_pct*100:>+9.2f}  {pf_str}  {r.avg_bars_held:>8.1f}"
        )

    # Honest interpretation
    h24 = results.get(24)
    print()
    if h24 and h24.total > 0:
        if h24.win_rate > 0.55 and h24.avg_pnl_pct > 0:
            print(f"  INTERPRETATION: At 24h horizon, WR={h24.win_rate*100:.1f}%, "
                  f"avg_pnl={h24.avg_pnl_pct*100:+.2f}% -> EDGE EXISTS")
        elif h24.avg_pnl_pct > 0:
            print(f"  INTERPRETATION: At 24h horizon, avg_pnl={h24.avg_pnl_pct*100:+.2f}% "
                  f"but WR={h24.win_rate*100:.1f}% (noisy / luck)")
        else:
            print(f"  INTERPRETATION: At 24h horizon, avg_pnl={h24.avg_pnl_pct*100:+.2f}% "
                  f"-> NO EDGE on this small sample ({h24.total} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
HyphyLiquid - backfill paper trade results.

For each paper trade in data/paper_trades.jsonl that has no
future_fills, fetch the subsequent price moves from the HL candle
data and compute what the trade would have returned.

This is the validation: after the live detector logs a signal, this
script fills in the price action so we can see if the signal was
actually right.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

LOG_PATH = PROJECT_ROOT / "data" / "paper_trades.jsonl"
CANDLE_DIR = PROJECT_ROOT / "data"
FILL_HORIZONS_HOURS = (1, 4, 24, 72)


def _load_candles(symbol: str) -> pd.DataFrame:
    """Load latest mainnet candles (assumes 90d was fetched earlier)."""
    path = CANDLE_DIR / f"{symbol.lower()}_candles_1h_90d_mainnet.csv"
    if not path.exists():
        return pd.DataFrame()
    c = pd.read_csv(path)
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
    return c.sort_values("timestamp").set_index("timestamp")


def _future_returns(candles: pd.DataFrame, signal_ts: pd.Timestamp) -> dict:
    """Return {horizon: return_pct} for each FILL_HORIZONS_HOURS, or None if out of data."""
    out = {}
    for h in FILL_HORIZONS_HOURS:
        target_ts = signal_ts + timedelta(hours=h)
        if signal_ts not in candles.index:
            return None
        entry = candles.loc[signal_ts, "close"]
        # Find the last available candle up to target
        available = candles[candles.index <= target_ts]
        if available.empty:
            out[h] = None
            continue
        exit_ = available.iloc[-1]["close"]
        out[h] = (exit_ - entry) / entry * 100  # pct
    return out


def main() -> int:
    if not LOG_PATH.exists():
        print(f"No paper trades at {LOG_PATH} yet.")
        return 0

    candles_by_symbol: dict[str, pd.DataFrame] = {}
    trades = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            trades.append(json.loads(line))

    filled = 0
    skipped = 0
    for t in trades:
        if t.get("future_fills") is not None:
            skipped += 1
            continue
        sym = t["symbol"]
        if sym not in candles_by_symbol:
            candles_by_symbol[sym] = _load_candles(sym)
        candles = candles_by_symbol[sym]
        if candles.empty:
            continue
        sig_ts = pd.Timestamp(t["signal_ts"])
        # Snap to nearest hour
        sig_ts = sig_ts.floor("h")
        rets = _future_returns(candles, sig_ts)
        if rets is None:
            continue
        # For a SHORT signal, profit = -return
        # For a LONG signal, profit = return
        sign = 1 if t["direction"] == "long" else -1
        t["future_fills"] = {
            f"+{h}h": {
                "raw_return_pct": rets[h],
                "trade_pnl_pct": rets[h] * sign if rets[h] is not None else None,
            }
            for h in FILL_HORIZONS_HOURS
        }
        filled += 1

    # Write back
    with LOG_PATH.open("w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")

    print(f"Filled: {filled}, skipped (already filled): {skipped}, total: {len(trades)}")
    if filled > 0:
        # Summary
        long_wins = short_wins = 0
        long_n = short_n = 0
        for t in trades:
            ff = t.get("future_fills")
            if not ff:
                continue
            pnl_24h = ff.get("+24h", {}).get("trade_pnl_pct")
            if pnl_24h is None:
                continue
            if t["direction"] == "long":
                long_n += 1
                if pnl_24h > 0:
                    long_wins += 1
            else:
                short_n += 1
                if pnl_24h > 0:
                    short_wins += 1
        print(f"  Long trades:  {long_wins}/{long_n} wins (24h horizon)")
        print(f"  Short trades: {short_wins}/{short_n} wins (24h horizon)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

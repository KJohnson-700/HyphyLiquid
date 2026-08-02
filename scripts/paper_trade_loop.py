"""
HyphyLiquid - paper trade loop.

Every iteration:
  1. Fetch the freshest HyperPerps snapshot for BTC and ETH
  2. Run the live cascade detector on each
  3. If a setup is detected, log a "would-have-traded" entry to
     data/paper_trades.jsonl with the state, signal, confidence, and
     the future-looking price moves (we'll fill these in by reading
     candles after the fact in backfill_paper_trades.py)

This runs as a daemon alongside poll_hyperperps.py.  It does NOT
place any real orders.

Run foreground:
    .\\venv\\Scripts\\python.exe scripts\\paper_trade_loop.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from src.strategy.live_cascade import detect_live_cascade, LiveSignal

API = "https://trade.hyperperps.app/api/public/heatmap/{symbol}"
INTERVAL_S = 300
LOG_PATH = PROJECT_ROOT / "data" / "paper_trades.jsonl"


def fetch_snapshot(symbol: str) -> dict | None:
    try:
        r = requests.get(API.format(symbol=symbol), timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  {symbol}: fetch error {e}", flush=True)
    return None


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"Paper-trade loop started at {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Logging to {LOG_PATH}", flush=True)
    print(f"Interval: {INTERVAL_S}s", flush=True)
    print(flush=True)

    while True:
        cycle_ts = datetime.now(timezone.utc).isoformat()
        any_signal = False
        for sym in ("BTC", "ETH"):
            snap = fetch_snapshot(sym)
            if snap is None:
                continue
            state = detect_live_cascade(sym, snap)
            if state.signal == LiveSignal.NO_SETUP:
                continue
            # We have a real signal - log it
            record = {
                "signal_ts": cycle_ts,
                "symbol": state.symbol,
                "direction": state.signal.value,
                "confidence": state.confidence,
                "spot_at_compute": state.spot_at_compute,
                "current_price": state.current_price,
                "cascade_distance_pct": state.cascade_distance_pct,
                "trapped_long_pct": state.trapped_long_pct,
                "trapped_short_pct": state.trapped_short_pct,
                "liq_within_2pct_long": state.liq_within_2pct_long,
                "liq_within_2pct_short": state.liq_within_2pct_short,
                "fresh_money_24h": state.fresh_money_24h,
                "reason": state.reason,
                "snapshot_age_seconds": snap.get("_meta", {}).get("age_seconds"),
                "sample_size": snap.get("sample_size"),
                # Future fills: filled by backfill_paper_trades.py
                "future_fills": None,
            }
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            any_signal = True
            print(
                f"  [{cycle_ts[11:19]}] {sym:3}  {state.signal.value:5}  "
                f"conf={state.confidence:.2f}  {state.reason}",
                flush=True,
            )
        if not any_signal:
            print(f"  [{cycle_ts[11:19]}] no signals (cycle quiet)", flush=True)
        next_wake = time.time() + INTERVAL_S
        time.sleep(max(0.0, next_wake - time.time()))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nPaper-trade loop stopped.", flush=True)
        sys.exit(0)

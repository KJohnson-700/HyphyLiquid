"""
HyphyLiquid - show the current live cascade state for BTC and ETH.

Fetches the freshest HyperPerps snapshot for each symbol, runs the
live cascade detector, and prints a clear summary.

Use this as a 'what's the setup right now?' tool. It does NOT place
any orders.

Run:
    .\\venv\\Scripts\\python.exe scripts\\live_cascade_state.py
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from src.strategy.live_cascade import detect_live_cascade, LiveSignal

API = "https://trade.hyperperps.app/api/public/heatmap/{symbol}"


def main() -> int:
    print("HyphyLiquid - Live Cascade State")
    print("=" * 60)
    print()

    any_setup = False
    for sym in ("BTC", "ETH"):
        try:
            r = requests.get(API.format(symbol=sym), timeout=15)
            snap = r.json()
        except Exception as e:
            print(f"  {sym}: API ERROR {e}")
            continue

        state = detect_live_cascade(sym, snap)
        meta = snap.get("_meta", {})
        marker = "!!" if state.signal != LiveSignal.NO_SETUP else "  "
        any_setup = any_setup or state.signal != LiveSignal.NO_SETUP

        print(f"{marker} {sym}  [{state.signal.value}]  conf={state.confidence:.2f}")
        print(f"   spot @ compute: ${state.spot_at_compute:,.2f}  "
              f"current: ${state.current_price:,.2f}  "
              f"distance: {state.cascade_distance_pct:+.2f}%")
        print(f"   snapshot age: {meta.get('age_seconds', '?')}s  "
              f"sample: {snap.get('sample_size', '?')} positions")
        print(f"   trapped: long={state.trapped_long_pct*100:.1f}%  short={state.trapped_short_pct*100:.1f}%")
        print(f"   near liq (2%):  long={state.liq_within_2pct_long*100:.1f}%  short={state.liq_within_2pct_short*100:.1f}%")
        print(f"   fresh money 24h: {state.fresh_money_24h:+.2f}")
        print(f"   reason: {state.reason}")
        print()

    if any_setup:
        print("A setup is active. Review manually before any action.")
    else:
        print("No cascade setup on either symbol right now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

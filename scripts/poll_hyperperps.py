"""
HyphyLiquid - HyperPerps snapshot poller.

Polls the free HyperPerps heatmap API every 5 minutes for BTC and ETH,
appends each snapshot to data/hyperperps/snapshots/YYYY-MM-DD.jsonl
(one JSON object per line, includes the snapshot timestamp and the
poll timestamp).

Run in background:
    Start-Process -FilePath '.\venv\Scripts\pythonw.exe' `
        -ArgumentList 'scripts\poll_hyperperps.py' -WindowStyle Hidden

Or foreground (Ctrl+C to stop):
    .\\venv\\Scripts\\python.exe scripts\\poll_hyperperps.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

API_URL = "https://trade.hyperperps.app/api/public/heatmap/{symbol}"
SYMBOLS = ("BTC", "ETH")
INTERVAL_S = 300  # 5 minutes, matches the API refresh cadence
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "hyperperps_snapshots"


def fetch(symbol: str) -> dict | None:
    try:
        r = requests.get(API_URL.format(symbol=symbol), timeout=20)
        if r.status_code == 200:
            return r.json()
        print(f"  {symbol}: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"  {symbol}: ERROR {e}", flush=True)
    return None


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Poller started at {datetime.now(timezone.utc).isoformat()}", flush=True)
    print(f"Saving snapshots to {SNAPSHOT_DIR}", flush=True)
    print(f"Interval: {INTERVAL_S}s, symbols: {SYMBOLS}", flush=True)
    print(flush=True)

    while True:
        poll_ts = datetime.now(timezone.utc)
        date_str = poll_ts.strftime("%Y-%m-%d")
        for sym in SYMBOLS:
            snap = fetch(sym)
            if snap is None:
                continue
            record = {
                "poll_ts": poll_ts.isoformat(),
                "snapshot": snap,
            }
            path = SNAPSHOT_DIR / f"{sym.lower()}_{date_str}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            meta = snap.get("_meta", {})
            age = meta.get("age_seconds", "?")
            sample = snap.get("sample_size", "?")
            cm = snap.get("cascade_mass", {})
            cm_long = cm.get("long", {}).get("within_2pct", 0)
            cm_short = cm.get("short", {}).get("within_2pct", 0)
            print(
                f"  [{poll_ts.strftime('%H:%M:%S')}] {sym}  "
                f"sample={sample}  age={age}s  "
                f"cascade 2%: long=${cm_long/1e6:.1f}M short=${cm_short/1e6:.1f}M",
                flush=True,
            )
        # Sleep until next interval
        next_wake = poll_ts.timestamp() + INTERVAL_S
        sleep_s = max(0.0, next_wake - time.time())
        time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nPoller stopped.", flush=True)
        sys.exit(0)

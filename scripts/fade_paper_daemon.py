"""Keep the funding-negative fade lane accumulating forward-paper trades.

The five bootstrap daemons all collect data; none runs the strategy that is
actually up for promotion. Without this loop the fade lane is frozen at
whatever trades already exist, so a lane can never reach Gate 2 (n>=30 with
>=15 forward trades, and >=2 market regimes).

Each tick, in order:
  1. rebuild the funding + candle panels from freshly captured WS data
  2. run paper_funding_neg_fade.py --mode paper (simulation only, no orders)
  3. re-label regimes on closed trades

Paper mode never places an order. Live trading in that script is gated
separately on an in-file flag plus a passed testnet bracket proof with a
visible protective stop; this daemon never passes --mode live_trading.

Usage:
  python3 scripts/fade_paper_daemon.py                # hourly
  python3 scripts/fade_paper_daemon.py --once         # single pass
  python3 scripts/fade_paper_daemon.py --interval 900
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PY = str(Path(sys.executable))

STEPS = [
    ("funding panel", [PY, "scripts/build_funding_panel.py"]),
    ("candle panel",  [PY, "scripts/build_candle_panel.py"]),
    ("fade paper",    [PY, "scripts/paper_funding_neg_fade.py", "--mode", "paper"]),
    ("regime labels", [PY, "scripts/label_trade_regimes.py"]),
]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def run_once() -> int:
    failures = 0
    for name, cmd in STEPS:
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                               text=True, timeout=1800)
            if r.returncode == 0:
                tail = [l for l in r.stdout.strip().splitlines() if l.strip()]
                print(f"  [{_ts()}] {name}: ok ({time.time()-t0:.0f}s) "
                      f"{tail[-1][:90] if tail else ''}", flush=True)
            else:
                failures += 1
                err = (r.stderr.strip().splitlines() or ["?"])[-1]
                print(f"  [{_ts()}] {name}: FAILED rc={r.returncode} {err[:140]}", flush=True)
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"  [{_ts()}] {name}: TIMEOUT", flush=True)
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()

    print(f"fade paper daemon starting (interval {args.interval}s, paper mode only)", flush=True)
    while True:
        print(f"[{_ts()}] tick", flush=True)
        run_once()
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

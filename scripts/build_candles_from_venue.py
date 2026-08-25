"""Backfill candle_panel.csv from Hyperliquid's candleSnapshot.

Companion to build_funding_from_venue.py. The jsonl builders only see what the
local WS daemons captured, so panel history dies with disk maintenance and the
lane cannot be re-simulated over any window the box did not personally record.
The venue serves the same hourly bars on demand, which makes the panel
reproducible from scratch and removes local retention from the critical path.

Locally captured rows win on overlap: they came from the same WS feed the
strategy consumes live, so keeping them preserves whatever microstructure the
venue's rollup smooths away.

Usage:
  python3 scripts/build_candles_from_venue.py
  python3 scripts/build_candles_from_venue.py --since 2026-08-01 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PANEL = PROJECT_ROOT / "data" / "candle_panel.csv"
API = "https://api.hyperliquid.xyz/info"
COLS = ["ts", "symbol", "open", "high", "low", "close", "volume"]


def _post(payload: dict, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"candleSnapshot failed after {retries} tries: {last}")


def fetch(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows, cursor, seen = [], start_ms, set()
    while cursor < end_ms:
        batch = _post({"type": "candleSnapshot", "req": {
            "coin": symbol, "interval": "1h",
            "startTime": cursor, "endTime": end_ms}})
        if not batch:
            break
        fresh = [b for b in batch if b["t"] not in seen]
        if not fresh:
            break
        rows.extend(fresh)
        seen.update(b["t"] for b in fresh)
        nxt = max(b["t"] for b in fresh) + 1
        if nxt <= cursor:
            break
        cursor = nxt
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "ts": pd.to_datetime(df.t, unit="ms", utc=True).dt.floor("h").dt.tz_localize(None),
        "symbol": symbol,
        "open": df.o.astype(float), "high": df.h.astype(float),
        "low": df.l.astype(float), "close": df.c.astype(float),
        "volume": df.v.astype(float),
    })
    return out.drop_duplicates(["ts", "symbol"], keep="last")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    existing = pd.DataFrame(columns=COLS)
    if PANEL.exists():
        existing = pd.read_csv(PANEL)
        existing["ts"] = pd.to_datetime(existing.ts)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               or sorted(existing.symbol.unique()))
    if not symbols:
        print("ERROR: no symbols; pass --symbols", file=sys.stderr)
        return 1

    start_ms = int(datetime.fromisoformat(args.since).replace(
        tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    got = []
    for sym in symbols:
        try:
            df = fetch(sym, start_ms, end_ms)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: FAILED {e}", file=sys.stderr)
            return 1
        print(f"  {sym}: {len(df)} venue candles")
        got.append(df)
    venue = pd.concat(got, ignore_index=True)

    before = len(existing)
    # local last => local wins on overlap
    combined = pd.concat([venue, existing], ignore_index=True)
    combined = combined.sort_values("ts").drop_duplicates(["ts", "symbol"], keep="last")
    combined = combined.sort_values(["symbol", "ts"])[COLS]
    print(f"\n{before} local + {len(venue)} venue -> {len(combined)} rows  "
          f"range {combined.ts.min()} -> {combined.ts.max()}")
    if args.dry_run:
        print("dry-run: not written")
        return 0
    combined.to_csv(PANEL, index=False)
    print(f"wrote {PANEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

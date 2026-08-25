"""Take funding_actual from Hyperliquid's fundingHistory, not polled snapshots.

asset_ctx.funding is the rate for the *upcoming* settlement, but the panel
builders stamp it with date_trunc('hour', poll_ts). Cross-correlating our panel
against the venue's own fundingHistory showed agreement peaking at a +1h shift
on every symbol (mean corr 0.635 at 0h vs 0.946 at +1h), i.e. the panel ran an
hour early and the lane entered a bar before the funding it was fading.

fundingHistory is the rate the venue actually applied, stamped with the hour it
applied to, so this removes the inference entirely. It is also re-fetchable on
demand, which means funding history no longer depends on local jsonl retention.

markPx / openInterest still come from the polled panel; only funding_actual is
overridden, and only where the venue has a value.

Usage:
  python3 scripts/build_funding_from_venue.py
  python3 scripts/build_funding_from_venue.py --since 2026-08-01 --dry-run
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
PANEL = PROJECT_ROOT / "data" / "funding_panel.csv"
API = "https://api.hyperliquid.xyz/info"


def _post(payload: dict, retries: int = 3) -> list:
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
    raise RuntimeError(f"fundingHistory failed after {retries} tries: {last}")


def fetch(symbol: str, start_ms: int) -> pd.DataFrame:
    """All hourly funding for one symbol since start_ms, paginated.

    The endpoint caps its response, so page forward on the last timestamp until
    it stops advancing -- otherwise long backfills silently truncate.
    """
    rows, cursor, seen = [], start_ms, set()
    while True:
        batch = _post({"type": "fundingHistory", "coin": symbol, "startTime": cursor})
        if not batch:
            break
        fresh = [b for b in batch if b["time"] not in seen]
        if not fresh:
            break
        rows.extend(fresh)
        seen.update(b["time"] for b in fresh)
        nxt = max(b["time"] for b in fresh) + 1
        if nxt <= cursor:
            break
        cursor = nxt
    if not rows:
        return pd.DataFrame(columns=["ts", "symbol", "funding_actual"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df.time, unit="ms", utc=True).dt.floor("h")
    df["funding_actual"] = df.fundingRate.astype(float)
    df["symbol"] = symbol
    return df[["ts", "symbol", "funding_actual"]].drop_duplicates(["ts", "symbol"], keep="last")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01", help="UTC date, YYYY-MM-DD")
    ap.add_argument("--symbols", default="", help="comma list; default = panel symbols")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not PANEL.exists():
        print(f"ERROR: {PANEL} not found; build the panel first", file=sys.stderr)
        return 1
    panel = pd.read_csv(PANEL)
    panel["ts"] = pd.to_datetime(panel.ts, utc=True)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               or sorted(panel.symbol.unique()))
    start_ms = int(datetime.fromisoformat(args.since).replace(
        tzinfo=timezone.utc).timestamp() * 1000)

    venue = []
    for sym in symbols:
        try:
            df = fetch(sym, start_ms)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: FAILED {e}", file=sys.stderr)
            return 1
        print(f"  {sym}: {len(df)} venue funding hours")
        venue.append(df)
    v = pd.concat(venue, ignore_index=True)

    merged = panel.merge(v, on=["ts", "symbol"], how="outer", suffixes=("", "_venue"))
    had = merged.funding_actual.notna()
    changed = int((had & merged.funding_actual_venue.notna()
                   & (merged.funding_actual - merged.funding_actual_venue).abs().gt(1e-12)).sum())
    added = int((~had & merged.funding_actual_venue.notna()).sum())
    # venue wins wherever it has a value; keep ours only where it does not
    merged["funding_actual"] = merged.funding_actual_venue.combine_first(merged.funding_actual)
    merged = merged.drop(columns=["funding_actual_venue"])
    merged = merged.sort_values(["symbol", "ts"]).drop_duplicates(["ts", "symbol"], keep="last")

    print(f"\n{changed} hours corrected, {added} hours added, {len(merged)} rows total")
    if args.dry_run:
        print("dry-run: not written")
        return 0
    merged.to_csv(PANEL, index=False)
    print(f"wrote {PANEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

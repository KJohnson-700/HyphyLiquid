"""
build_candle_panel.py — build a per-symbol hourly candle panel from ws_candle jsonl files.

Output: data/candle_panel.csv with columns:
  ts (hour), symbol, open, high, low, close, volume

1m candles are resampled to 1h. First close-of-hour becomes the hourly open.

Incremental by default (only processes today UTC, or last 3 days if today is empty).
Use --full to rebuild all files (slow but correct).

Optimized: per-file streaming aggregation to avoid building intermediate record lists.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd

import sys as _sys; _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.data_files import iter_data_files, open_data_file, open_data_file_binary, data_stem

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WS_CANDLE_DIR = PROJECT_ROOT / "data" / "ws_candle"
OUT_FILE = PROJECT_ROOT / "data" / "candle_panel.csv"


def _symbol_from_stem(stem: str) -> str:
    m = re.match(r"^([a-z]+)_([a-z0-9]+)_(\d{4}-\d{2}-\d{2})$", stem)
    if m:
        return f"xyz:{m.group(2).upper()}"
    return stem.split("_")[0].upper()


def _aggregate_file(f: Path) -> list[dict]:
    """Stream-process one file, return list of hourly OHLC dicts.

    Per-hour state: open (first), high (max), low (min), close (last), volume (sum).
    """
    symbol = _symbol_from_stem(data_stem(f))
    # Per-hour: ts_hour -> [open, high, low, close, volume]
    state: dict = {}
    try:
        with open_data_file(f) as fp:
            for line in fp:
                # Fast parse: avoid json.loads for performance
                # Find "t":<num>, "o":<num>, "h":<num>, "l":<num>, "c":<num>, "v":<num>
                # Use json.loads (no shortcut parser available without orjson)
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                p = rec.get("payload")
                if not p:
                    continue
                try:
                    ts_ms = int(p.get("t", 0))
                except Exception:
                    continue
                if not ts_ms:
                    continue
                # Floor to hour (UTC)
                ts_hour = pd.Timestamp(ts_ms, unit="ms", tz="UTC").floor("h")
                # Read values (defensive)
                try:
                    o = float(p["o"])
                    h = float(p["h"])
                    l = float(p["l"])
                    c = float(p["c"])
                    v = float(p.get("v", 0))
                except Exception:
                    continue
                if ts_hour in state:
                    entry = state[ts_hour]
                    if h > entry[1]:
                        entry[1] = h
                    if l < entry[2]:
                        entry[2] = l
                    entry[3] = c  # close
                    entry[4] += v
                else:
                    state[ts_hour] = [o, h, l, c, v]
    except Exception as e:
        print(f"  WARN: {f.name}: {e}", flush=True)
    # Convert to records
    out = []
    for ts_hour, (o, h, l, c, v) in state.items():
        out.append({
            "ts": ts_hour,
            "symbol": symbol,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="Full rebuild (slow, all files)")
    ap.add_argument("--date", help="Process a specific date (YYYY-MM-DD)")
    args = ap.parse_args()

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_utc = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    target_date = args.date or today_utc

    if args.full:
        files = iter_data_files(WS_CANDLE_DIR, "*.jsonl")
        print(f"scanning {len(files)} ws_candle files (full rebuild)...", flush=True)
    else:
        # Incremental: today's + yesterday's files (so we don't miss the latest bar)
        files = sorted(set(
            iter_data_files(WS_CANDLE_DIR, f"*_{target_date}.jsonl") +
            iter_data_files(WS_CANDLE_DIR, f"*_{yesterday_utc}.jsonl")
        ))
        print(f"scanning {len(files)} ws_candle files for {yesterday_utc} + {target_date} (incremental)...", flush=True)
        if not files:
            print(f"  no files, falling back to last 3 days", flush=True)
            all_dates = sorted({data_stem(f).split("_")[-1] for f in iter_data_files(WS_CANDLE_DIR, "*.jsonl")})
            for d in all_dates[-3:]:
                files.extend(iter_data_files(WS_CANDLE_DIR, f"*_{d}.jsonl"))
            files = sorted(set(files))

    import time
    start = time.time()
    all_rows = []
    for f in files:
        t0 = time.time()
        rows = _aggregate_file(f)
        all_rows.extend(rows)
        print(f"  {f.name}: {len(rows)} hourly rows in {time.time()-t0:.1f}s", flush=True)
    print(f"  total: {len(all_rows)} hourly rows in {time.time()-start:.1f}s", flush=True)

    if not all_rows:
        print("no rows collected", flush=True)
        return
    new_hourly = pd.DataFrame(all_rows)

    # Merge with existing panel
    if OUT_FILE.exists() and not args.full:
        try:
            existing = pd.read_csv(OUT_FILE, parse_dates=["ts"])
            if existing["ts"].dt.tz is not None:
                existing["ts"] = existing["ts"].dt.tz_localize(None)
            if new_hourly["ts"].dt.tz is not None:
                new_hourly = new_hourly.copy()
                new_hourly["ts"] = new_hourly["ts"].dt.tz_localize(None)
            combined = pd.concat([existing, new_hourly], ignore_index=True)
            combined = combined.sort_values("ts").drop_duplicates(["ts", "symbol"], keep="last")
            out_df = combined
        except Exception as e:
            print(f"  WARN: could not load existing panel: {e}", flush=True)
            out_df = new_hourly
    else:
        out_df = new_hourly

    out_df = out_df.sort_values(["ts", "symbol"]).reset_index(drop=True)
    out_df.to_csv(OUT_FILE, index=False)
    print(f"wrote {OUT_FILE}  rows={len(out_df)}  symbols={out_df['symbol'].nunique()}  range={out_df['ts'].min()} -> {out_df['ts'].max()}", flush=True)


if __name__ == "__main__":
    main()

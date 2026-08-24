"""
build_funding_panel.py — build a per-symbol hourly funding panel from asset_ctx jsonl files
AND the WS activeAssetCtx channel (data/ws_activeAssetCtx/).

Sources (in order of preference):
  1. data/ws_activeAssetCtx/{sym}_{date}.jsonl  — WS channel, more recent and reliable
  2. data/asset_ctx/{sym}_{date}.jsonl           — REST poller (legacy)

Output: data/funding_panel.csv with columns:
  ts (hour), symbol, funding_actual, funding_predicted, markPx, openInterest

Each row is one (timestamp, symbol) pair. The 'funding_actual' is the most recent
paid funding rate. Both as decimal (e.g., 0.0000125).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_CTX_DIR = PROJECT_ROOT / "data" / "asset_ctx"
WS_ACTIVE_ASSET_CTX_DIR = PROJECT_ROOT / "data" / "ws_activeAssetCtx"
OUT_FILE = PROJECT_ROOT / "data" / "funding_panel.csv"


def _symbol_from_stem(stem: str) -> str:
    """Convert filename stem to canonical symbol.
    HIP-3: 'xyz_gold_2026-08-04' -> 'xyz:GOLD'
    Regular: 'btc_2026-08-10' -> 'BTC'
    """
    m = re.match(r"^([a-z]+)_([a-z]+)_(\d{4}-\d{2}-\d{2})$", stem)
    if m:
        return f"xyz:{m.group(2).upper()}"
    return stem.split("_")[0].upper()


def _read_rest_asset_ctx() -> list[dict]:
    """Read from data/asset_ctx/ (REST poller, legacy)."""
    rows = []
    files = sorted(ASSET_CTX_DIR.glob("*.jsonl"))
    if not files:
        return rows
    print(f"  [REST] scanning {len(files)} asset_ctx files...", flush=True)
    for f in files:
        symbol = _symbol_from_stem(f.stem)
        try:
            with f.open("r", encoding="utf-8") as fp:
                for line in fp:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = rec.get("poll_ts", "")
                    if not ts:
                        continue
                    payload_sym = rec.get("symbol", "")
                    if payload_sym and ":" in payload_sym:
                        symbol = payload_sym
                    ts_hour = pd.Timestamp(ts).floor("h")
                    ctx = rec.get("context", {})
                    pred = rec.get("predicted", {}).get("HlPerp", {})
                    try:
                        rows.append({
                            "ts": ts_hour,
                            "symbol": symbol,
                            "funding_actual": float(ctx.get("funding", 0) or 0),
                            "funding_predicted": float(pred.get("fundingRate", 0) or 0),
                            "markPx": float(ctx.get("markPx", 0) or 0),
                            "openInterest": float(ctx.get("openInterest", 0) or 0),
                        })
                    except Exception:
                        continue
        except Exception as e:
            print(f"    WARN: {f.name}: {e}", flush=True)
    print(f"  [REST] collected {len(rows)} rows", flush=True)
    return rows


def _read_ws_active_asset_ctx() -> list[dict]:
    """Read from data/ws_activeAssetCtx/ (WS channel, primary).
    Performance: read only the last ~5KB of each file to find the latest record per file.
    This gives us 1 row per (symbol, date) — the latest funding state. For a full
    per-hour panel, use a longer history load.
    """
    rows = []
    files = sorted(WS_ACTIVE_ASSET_CTX_DIR.glob("*.jsonl"))
    if not files:
        return rows
    print(f"  [WS] scanning {len(files)} ws_activeAssetCtx files (last 5KB each)...", flush=True)
    TAIL_BYTES = 5000
    for f in files:
        symbol = _symbol_from_stem(f.stem)
        try:
            with f.open("rb") as fp:
                fp.seek(0, 2)  # seek to end
                file_size = fp.tell()
                # Read last TAIL_BYTES (or whole file if smaller)
                read_size = min(TAIL_BYTES, file_size)
                fp.seek(file_size - read_size)
                tail = fp.read().decode("utf-8", errors="ignore")
            # Find the LAST valid line with funding data
            lines = tail.split("\n")
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                payload = rec.get("payload", {})
                if not payload:
                    continue
                ts_ms = payload.get("ts")
                if not ts_ms:
                    continue
                try:
                    ts = pd.to_datetime(int(ts_ms), unit="ms", utc=True)
                except Exception:
                    continue
                ts_hour = ts.floor("h")
                payload_sym = payload.get("coin", "")
                if payload_sym and ":" in str(payload_sym):
                    symbol = str(payload_sym)
                ctx = payload.get("ctx", {}) or {}
                if not isinstance(ctx, dict):
                    continue
                if not ctx.get("funding") and not ctx.get("markPx"):
                    continue
                try:
                    rows.append({
                        "ts": ts_hour,
                        "symbol": symbol,
                        "funding_actual": float(ctx.get("funding", 0) or 0),
                        "funding_predicted": 0.0,
                        "markPx": float(ctx.get("markPx", 0) or 0),
                        "openInterest": float(ctx.get("openInterest", 0) or 0),
                    })
                except Exception:
                    continue
                break  # Got the latest valid record
        except Exception as e:
            print(f"    WARN: {f.name}: {e}", flush=True)
    print(f"  [WS] collected {len(rows)} rows (1 per file, latest)", flush=True)
    return rows


def _read_ws_active_asset_ctx_full() -> list[dict]:
    """Read all hours from data/ws_activeAssetCtx/. Slow path — only use for full rebuild."""
    rows = []
    files = sorted(WS_ACTIVE_ASSET_CTX_DIR.glob("*.jsonl"))
    if not files:
        return rows
    print(f"  [WS-full] scanning {len(files)} ws_activeAssetCtx files (full)...", flush=True)
    for f in files:
        symbol = _symbol_from_stem(f.stem)
        last_per_hour: dict = {}
        try:
            with f.open("r", encoding="utf-8") as fp:
                for line in fp:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    payload = rec.get("payload", {})
                    if not payload:
                        continue
                    ts_ms = payload.get("ts")
                    if not ts_ms:
                        continue
                    try:
                        ts = pd.to_datetime(int(ts_ms), unit="ms", utc=True)
                    except Exception:
                        continue
                    ts_hour = ts.floor("h")
                    payload_sym = payload.get("coin", "")
                    if payload_sym and ":" in str(payload_sym):
                        symbol = str(payload_sym)
                    ctx = payload.get("ctx", {}) or {}
                    if not isinstance(ctx, dict):
                        continue
                    if not ctx.get("funding") and not ctx.get("markPx"):
                        continue
                    try:
                        last_per_hour[ts_hour] = {
                            "ts": ts_hour,
                            "symbol": symbol,
                            "funding_actual": float(ctx.get("funding", 0) or 0),
                            "funding_predicted": 0.0,
                            "markPx": float(ctx.get("markPx", 0) or 0),
                            "openInterest": float(ctx.get("openInterest", 0) or 0),
                        }
                    except Exception:
                        continue
        except Exception as e:
            print(f"    WARN: {f.name}: {e}", flush=True)
        rows.extend(last_per_hour.values())
    print(f"  [WS-full] collected {len(rows)} rows", flush=True)
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="Full rebuild (slow, all hours from WS files)")
    args = ap.parse_args()

    rows = []
    # WS is the primary source (more reliable, fresher)
    print("Reading funding data sources...", flush=True)
    if args.full:
        rows.extend(_read_ws_active_asset_ctx_full())
    else:
        rows.extend(_read_ws_active_asset_ctx())
    rows.extend(_read_rest_asset_ctx())
    if not rows:
        print("no rows collected from any source", flush=True)
        return
    df_new = pd.DataFrame(rows)
    # Dedup new rows
    df_new = df_new.sort_values("ts").drop_duplicates(["ts", "symbol"], keep="last")

    # Merge with existing panel (preserves history)
    if OUT_FILE.exists() and not args.full:
        try:
            existing = pd.read_csv(OUT_FILE, parse_dates=["ts"])
            # Combine
            combined = pd.concat([existing, df_new], ignore_index=True)
            combined = combined.sort_values("ts").drop_duplicates(["ts", "symbol"], keep="last")
            df = combined
        except Exception as e:
            print(f"  WARN: could not load existing panel: {e}", flush=True)
            df = df_new
    else:
        df = df_new

    df.to_csv(OUT_FILE, index=False)
    print(f"wrote {OUT_FILE}  rows={len(df)}  symbols={df['symbol'].nunique()}  range={df['ts'].min()} -> {df['ts'].max()}", flush=True)
    print(f"\n=== per-symbol funding stats (actual) ===", flush=True)
    stats = df.groupby("symbol")["funding_actual"].agg(["count", "mean", "std", "min", "max"])
    print(stats.to_string(), flush=True)


if __name__ == "__main__":
    main()

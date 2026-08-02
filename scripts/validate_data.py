"""Validate the new data layer for the fade_or_follow spec.

Per the user's Task 1 (validate the new data is actually usable):
  1. data/ws_candle/ is writing 1m candles for BTC/ETH/SOL/HYPE
  2. asset_ctx is really updating every 60s
  3. event_features.jsonl has one row per liquidation event
  4. timestamp alignment: event time vs nearest candle vs book vs OI
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA = PROJECT_ROOT / "data"


def _age_seconds(path: Path) -> float:
    if not path.exists():
        return -1.0
    return (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))


def _read_last(path: Path) -> dict | None:
    if not path.exists():
        return None
    last = None
    for line in path.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def check_1m_candles() -> dict:
    out = {}
    for sym in ("btc", "eth", "sol", "hype"):
        files = list((DATA / "ws_candle").glob(f"{sym}_2026-08-02.jsonl"))
        if not files:
            out[sym] = {"lines": 0, "age_s": -1, "last_ts": None}
            continue
        f = files[0]
        n = _line_count(f)
        last = _read_last(f)
        payload = last.get("payload") if isinstance(last, dict) else {}
        out[sym] = {
            "lines": n,
            "age_s": round(_age_seconds(f), 1),
            "last_candle_t": payload.get("t"),
            "last_candle_close": payload.get("c"),
            "last_candle_interval": payload.get("i"),
        }
    return out


def check_asset_ctx_cadence() -> dict:
    """Check the most recent asset_ctx records and report inter-arrival times."""
    out = {}
    for sym in ("btc", "eth", "sol", "hype"):
        f = DATA / "asset_ctx" / f"{sym}_2026-08-02.jsonl"
        if not f.exists():
            out[sym] = {"lines": 0, "age_s": -1, "inter_arrival_s": None}
            continue
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        n = len(lines)
        if n < 2:
            out[sym] = {"lines": n, "age_s": round(_age_seconds(f), 1), "inter_arrival_s": None}
            continue
        last = json.loads(lines[-1])
        prev = json.loads(lines[-2])
        last_ts = datetime.fromisoformat(last["poll_ts"]).timestamp()
        prev_ts = datetime.fromisoformat(prev["poll_ts"]).timestamp()
        out[sym] = {
            "lines": n,
            "age_s": round(_age_seconds(f), 1),
            "inter_arrival_s": round(last_ts - prev_ts, 1),
            "last_poll_ts": last["poll_ts"],
        }
    return out


def check_event_features() -> dict:
    liq = _line_count(DATA / "liquidations.jsonl")
    feat = _line_count(DATA / "event_features.jsonl")
    out = {"liquidations": liq, "event_features": feat, "ratio": feat / max(liq, 1)}
    # Last 5 event features for spot check
    feat_path = DATA / "event_features.jsonl"
    if feat_path.exists():
        last5 = []
        for line in feat_path.open("r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                last5.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out["last_5"] = last5[-5:]
    return out


def check_timestamp_alignment() -> dict:
    """Pick a real 2026 liquidation event with full coverage, report
    alignment offsets: event_ts vs nearest l2book, asset_ctx, and 1m candle.
    Coverage: l2book from 02:49 UTC, asset_ctx from 02:48 UTC, 1m candles
    from 04:11 UTC (post-WS-restart). Use an event after 04:11 for full
    coverage of all three sources.
    """
    liq_path = DATA / "liquidations.jsonl"
    if not liq_path.exists():
        return {"error": "no liquidations.jsonl"}
    # Load all 2026 events
    events = []
    for line in liq_path.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts", "")
        if "2026-08-02" in ts and rec.get("symbol") == "BTC":
            events.append(rec)
    if not events:
        return {"error": "no 2026 BTC events"}
    # Pick an event that has all three coverage windows. First 1m candle
    # was at 04:11:13 UTC, so we need events >= 04:12:00 to be safe.
    events.sort(key=lambda e: e["ts"])
    candidates = [e for e in events if e["ts"] >= "2026-08-02T04:12:00"]
    if not candidates:
        return {"error": "no post-04:12 events for full alignment check"}
    chosen = candidates[-1]
    ets_ms = int(datetime.fromisoformat(chosen["ts"]).timestamp() * 1000)
    out = {
        "event": {
            "ts": chosen["ts"],
            "symbol": chosen["symbol"],
            "side": chosen["side"],
            "event_vwap": chosen.get("price_avg"),
            "total_notional": chosen.get("total_notional"),
        }
    }
    # Find nearest l2book
    l2_path = DATA / "ws_l2book" / f"{chosen['symbol'].lower()}_2026-08-02.jsonl"
    if l2_path.exists():
        best = None
        for line in l2_path.open("r", encoding="utf-8"):
            try:
                rec = json.loads(line.strip())
            except (json.JSONDecodeError, KeyError):
                continue
            t = rec.get("payload", {}).get("time")
            if t is None:
                continue
            if best is None or abs(t - ets_ms) < abs(best["t"] - ets_ms):
                best = {"t": t, "recv_ts": rec.get("recv_ts"), "levels_count": len(rec.get("payload", {}).get("levels", []))}
        if best:
            out["l2_nearest"] = best
            out["l2_delta_s"] = round((best["t"] - ets_ms) / 1000.0, 1)
    # Find nearest asset_ctx
    ctx_path = DATA / "asset_ctx" / f"{chosen['symbol'].lower()}_2026-08-02.jsonl"
    if ctx_path.exists():
        best = None
        for line in ctx_path.open("r", encoding="utf-8"):
            try:
                rec = json.loads(line.strip())
            except (json.JSONDecodeError, KeyError):
                continue
            ts = rec.get("poll_ts")
            if not ts:
                continue
            try:
                t = int(datetime.fromisoformat(ts).timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            if best is None or abs(t - ets_ms) < abs(best["t"] - ets_ms):
                best = {"t": t, "poll_ts": ts, "oi": rec.get("context", {}).get("openInterest")}
        if best:
            out["ctx_nearest"] = best
            out["ctx_delta_s"] = round((best["t"] - ets_ms) / 1000.0, 1)
    # Find nearest 1m candle
    candle_path = DATA / "ws_candle" / f"{chosen['symbol'].lower()}_2026-08-02.jsonl"
    if candle_path.exists():
        best = None
        for line in candle_path.open("r", encoding="utf-8"):
            try:
                rec = json.loads(line.strip())
            except (json.JSONDecodeError, KeyError):
                continue
            t = rec.get("payload", {}).get("t")
            if t is None:
                continue
            if best is None or abs(t - ets_ms) < abs(best["t"] - ets_ms):
                best = {"t": t, "close": rec.get("payload", {}).get("c"), "interval": rec.get("payload", {}).get("i")}
        if best:
            out["candle_nearest"] = best
            out["candle_delta_s"] = round((best["t"] - ets_ms) / 1000.0, 1)
    return out


def main() -> int:
    print("=" * 60)
    print("DATA-LAYER VALIDATION (Task 1 of the spec build order)")
    print("=" * 60)

    print("\n--- 1m candles (data/ws_candle/) ---")
    candles = check_1m_candles()
    for sym, d in candles.items():
        print(f"  {sym.upper():5} lines={d['lines']:>5}  age={d['age_s']:>6.1f}s  "
              f"interval={d.get('last_candle_interval','-')}  close={d.get('last_candle_close','-')}")

    print("\n--- asset_ctx cadence (target 60s) ---")
    ctx = check_asset_ctx_cadence()
    for sym, d in ctx.items():
        print(f"  {sym.upper():5} lines={d['lines']:>5}  age={d['age_s']:>6.1f}s  "
              f"inter_arrival={d.get('inter_arrival_s', '-'):>5}s")

    print("\n--- event_features.jsonl vs liquidations.jsonl ---")
    feat = check_event_features()
    print(f"  liquidations: {feat['liquidations']}")
    print(f"  event_features: {feat['event_features']}")
    print(f"  ratio: {feat['ratio']:.3f}  (target: 1.0)")
    if feat["event_features"] > 0 and feat["event_features"] <= 5:
        print("  Last 5 event features (raw):")
        for f in feat.get("last_5", []):
            print(f"    {f.get('event_ts')}  {f.get('symbol')}  {f.get('side')}  vwap={f.get('event_vwap')}")

    print("\n--- timestamp alignment (most recent event) ---")
    align = check_timestamp_alignment()
    if "error" in align:
        print(f"  {align['error']}")
    else:
        ev = align["event"]
        print(f"  event:  ts={ev['ts']}  {ev['symbol']}  {ev['side']}  vwap={ev['event_vwap']}")
        print(f"  l2_source_ts:        {align.get('l2_source_ts')}  delta={align.get('l2_delta_s','-')}s")
        print(f"  ctx_source_poll_ts:  {align.get('ctx_source_poll_ts')}  delta={align.get('ctx_delta_s','-')}s")
        if "nearest_candle_t" in align:
            print(f"  nearest_candle_t:    {align['nearest_candle_t']}  delta={align['nearest_candle_delta_s']}s")

    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
HyphyLiquid - Multi-channel public WebSocket collector.

Subscribes to public, no-auth WebSocket channels for BTC, ETH, SOL, HYPE, DOGE, BNB:
  - trades         (every public trade, real-time)
  - l2Book         (top-of-book depth, 10 levels, real-time)
  - candle         (1m candle updates — needed for scalp time horizons)
  - activeAssetCtx (mark, oracle, funding, OI per coin, real-time)
  - bbo            (best bid/offer only, lighter than l2Book)

Saves each channel's data to its own directory:
  data/ws_trades/{sym}_{date}.jsonl
  data/ws_l2book/{sym}_{date}.jsonl
  data/ws_candle/{sym}_{date}.jsonl
  data/ws_asset_ctx/{sym}_{date}.jsonl
  data/ws_bbo/{sym}_{date}.jsonl

For 1h historical candles (regime detection, band features), use
scripts/fetch_historical.py — that pulls 1h backfill via REST.
The WS `candle` channel is 1m-only here for the live scalp-grade data.

Run foreground (Ctrl+C to stop):
    .\\venv\\Scripts\\python.exe scripts\\collect_ws_data.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import websocket  # from hyperliquid SDK's deps

WS_URL = "wss://api.hyperliquid.xyz/ws"
# v1 trade symbols (active execution): BTC, ETH
# research symbols (data collection only): SOL, HYPE, DOGE, BNB
# hip3 probe (2026-08-03): XYZ:GOLD, XYZ:SILVER — research-only, no execution
SYMBOLS = ("BTC", "ETH", "SOL", "HYPE", "DOGE", "BNB", "XYZ:GOLD", "XYZ:SILVER")
RECONNECT_DELAY_S = 5
DATA_ROOT = PROJECT_ROOT / "data"


def _channel_dir(channel: str) -> Path:
    p = DATA_ROOT / f"ws_{channel}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_trade_legacy_format(symbol: str, t: dict) -> None:
    """Write trade in the legacy format that collect_trades.py / liquidation_monitor.py expects.
    Format: {key, snapshot_ts, trade: {coin, side, px, sz, time, tid, users}}"""
    trades_dir = DATA_ROOT / "trades"
    trades_dir.mkdir(parents=True, exist_ok=True)
    ts_ms = t.get("time") or int(datetime.now(timezone.utc).timestamp() * 1000)
    date_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    path = trades_dir / f"{symbol.lower()}_{date_str}.jsonl"
    tid = t.get("tid")
    key = str(tid) if tid is not None else f"{t.get('hash', '')}_{ts_ms}"
    record = {
        "key": key,
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "trade": {
            "coin": t.get("coin"),
            "side": t.get("side"),
            "px": t.get("px"),
            "sz": t.get("sz"),
            "time": ts_ms,
            "tid": tid,
            "hash": t.get("hash"),
            "users": t.get("users"),
        },
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _save(channel: str, symbol: str, payload: dict) -> None:
    d = _channel_dir(channel)
    ts_ms = payload.get("ts") or int(datetime.now(timezone.utc).timestamp() * 1000)
    date_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    path = d / f"{symbol.lower()}_{date_str}.jsonl"
    record = {"recv_ts": datetime.now(timezone.utc).isoformat(), "payload": payload}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


_last_summary: dict[str, float] = {}


def _maybe_log(channel: str, symbol: str) -> None:
    """Print a per-channel-per-symbol summary every 30s."""
    key = f"{channel}:{symbol}"
    now = time.time()
    if now - _last_summary.get(key, 0) < 30:
        return
    _last_summary[key] = now
    # Count today's records for this key
    d = _channel_dir(channel)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = d / f"{symbol.lower()}_{date_str}.jsonl"
    if path.exists():
        lines = sum(1 for _ in path.open("r", encoding="utf-8"))
        print(f"  [{channel}] {symbol}: {lines} records today", flush=True)


def on_message(ws, message):
    import traceback
    try:
        data = json.loads(message)
    except Exception:
        return
    # If a list comes through, log it once and skip
    if isinstance(data, list):
        print(f"  [debug] list message: {str(data)[:200]}", flush=True)
        return
    try:
        channel = data.get("channel")
    except AttributeError as e:
        print(f"  [err] data.get('channel'): {e}; data type={type(data).__name__}; sample={str(data)[:200]}", flush=True)
        return
    except Exception as e:
        print(f"  [err] channel extraction: {e}\n{traceback.format_exc()}", flush=True)
        return
    if channel == "pong":
        return
    if channel in ("subscriptionResponse", "error"):
        return
    try:
        payload = data.get("data")
    except AttributeError as e:
        print(f"  [err] data.get('data'): {e}; data type={type(data).__name__}; sample={str(data)[:200]}", flush=True)
        return
    except Exception as e:
        print(f"  [err] data extraction: {e}\n{traceback.format_exc()}", flush=True)
        return
    if payload is None:
        return

    # Debug: log unknown channels
    if channel not in ("trades", "candle", "activeAssetCtx", "bbo", "l2Book"):
        print(f"  [debug] unknown channel={channel!r}, data type={type(payload).__name__}, sample={str(payload)[:300]}", flush=True)
        return
    # Diagnostic: show one sample of the payload structure for each channel
    # the first time we see it, so the field-name assumptions stay honest.
    if not hasattr(on_message, "_seen_channels"):
        on_message._seen_channels = set()  # type: ignore[attr-defined]
    if channel not in on_message._seen_channels:  # type: ignore[attr-defined]
        on_message._seen_channels.add(channel)  # type: ignore[attr-defined]
        print(f"  [schema] first {channel} payload: {str(payload)[:400]}", flush=True)

    # Per-channel handling
    try:
        if channel == "trades":
            for t in payload:
                if not isinstance(t, dict):
                    continue
                coin = t.get("coin", "").upper()
                if coin in SYMBOLS:
                    t["ts"] = t.get("time") or int(datetime.now(timezone.utc).timestamp() * 1000)
                    _save("trades", coin, t)
                    _save_trade_legacy_format(coin, t)
                    _maybe_log("trades", coin)
        elif channel == "candle":
            # Candle payload is a SINGLE dict (not a list like trades).
            # The handler used to iterate as if it were a list, which made
            # the dict's string keys trip the isinstance(dict) check and
            # silently drop every record. See 2026-08-02 hot-fix.
            c = payload if isinstance(payload, dict) else None
            if c is not None:
                coin = (c.get("s") or c.get("coin") or c.get("sym") or "").upper()
                if coin in SYMBOLS:
                    c["ts"] = c.get("t") or int(datetime.now(timezone.utc).timestamp() * 1000)
                    c["coin"] = coin
                    _save("candle", coin, c)
                    _maybe_log("candle", coin)
        elif channel in ("activeAssetCtx", "bbo"):
            if not isinstance(payload, dict):
                return
            coin = payload.get("coin", "").upper()
            if coin in SYMBOLS:
                payload["ts"] = int(datetime.now(timezone.utc).timestamp() * 1000)
                _save(channel, coin, payload)
                _maybe_log(channel, coin)
        elif channel == "l2Book":
            if not isinstance(payload, dict):
                return
            coin = payload.get("coin", "").upper()
            if coin in SYMBOLS:
                payload["ts"] = int(datetime.now(timezone.utc).timestamp() * 1000)
                _save("l2book", coin, payload)
                _maybe_log("l2book", coin)
    except Exception as e:
        print(f"  [err] {channel} handler: {e}\n{traceback.format_exc()}", flush=True)


def on_open(ws):
    print(f"[WS] Connected to {WS_URL}", flush=True)
    for sym in SYMBOLS:
        for channel, extra in [
            ("trades", {}),
            ("l2Book", {"nSigFigs": 5}),
            # 1m candles (live scalp-grade); 1h history comes from
            # scripts/fetch_historical.py (REST backfill).
            ("candle", {"interval": "1m"}),
            ("activeAssetCtx", {}),
            ("bbo", {}),
        ]:
            sub = {"type": channel, "coin": sym}
            sub.update(extra)
            ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
            print(f"  subscribed: {channel} {sym}{(' ' + str(extra)) if extra else ''}", flush=True)


def on_error(ws, error):
    # Silently swallow all errors. The lib occasionally throws an internal
    # "list object has no attribute get" error which is harmless - data
    # collection continues fine. If something serious happens, the data
    # files will stop growing.
    pass


def on_close(ws, code, msg):
    print(f"[WS] Closed: code={code} msg={msg}", flush=True)


def main() -> int:
    print("HyphyLiquid - Multi-Channel Public WebSocket Collector")
    print(f"Channels: trades, l2Book, candle, activeAssetCtx, bbo")
    print(f"Symbols: {SYMBOLS}")
    print(f"Data dir: {DATA_ROOT}")
    print()
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever()
        except Exception as e:
            print(f"[WS] Crashed: {e}", flush=True)
        print(f"[WS] Reconnecting in {RECONNECT_DELAY_S}s...", flush=True)
        time.sleep(RECONNECT_DELAY_S)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nWS collector stopped.")
        sys.exit(0)

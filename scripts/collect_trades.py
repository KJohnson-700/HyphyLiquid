"""
HyphyLiquid - Public trade collector for BTC and ETH.

Two collection methods:
  1. WebSocket `trades` subscription (real-time, no auth, public)
  2. REST `recentTrades` polling (last 10 trades, no auth, public)

Both save trades to data/trades/btc_YYYY-MM-DD.jsonl and eth_YYYY-MM-DD.jsonl
with one JSON object per line including the snapshot timestamp.

Run foreground (Ctrl+C to stop):
    .\\venv\\Scripts\\python.exe scripts\\collect_trades.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests
import websocket  # from hyperliquid SDK's deps

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
SYMBOLS = ("BTC", "ETH")
REST_POLL_INTERVAL_S = 30  # how often to poll recentTrades as a safety net
TRADE_DIR = PROJECT_ROOT / "data" / "trades"


def fetch_recent_trades(symbol: str) -> list[dict]:
    try:
        r = requests.post(
            INFO_URL,
            json={"type": "recentTrades", "coin": symbol},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  [{symbol}] REST error: {e}", flush=True)
    return []


def _trade_path(symbol: str, date_str: str) -> Path:
    return TRADE_DIR / f"{symbol.lower()}_{date_str}.jsonl"


def _save_trade(symbol: str, trade: dict) -> bool:
    """Save a single trade, return True if it was new (not duplicate)."""
    TRADE_DIR.mkdir(parents=True, exist_ok=True)
    # Use tid + hash as dedup key
    tid = trade.get("tid")
    if tid is None:
        # Fall back to hash + time
        key = f"{trade.get('hash', '')}_{trade.get('time', '')}"
    else:
        key = str(tid)
    path = _trade_path(symbol, datetime.fromtimestamp(
        trade["time"] / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d"))
    # Quick dedup: check last 50 lines
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip().splitlines()[-50:]
        for line in existing:
            try:
                existing_key = json.loads(line).get("key")
                if existing_key == key:
                    return False
            except Exception:
                continue
    record = {
        "key": key,
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "trade": trade,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return True


def _rest_loop() -> None:
    """Background REST poller (safety net for WS misses)."""
    print("[REST] Polling recentTrades every 30s as a safety net...", flush=True)
    while True:
        for sym in SYMBOLS:
            trades = fetch_recent_trades(sym)
            new_count = 0
            for t in trades:
                if _save_trade(sym, t):
                    new_count += 1
            if new_count:
                print(f"  [REST] {sym}: {new_count} new trades", flush=True)
        time.sleep(REST_POLL_INTERVAL_S)


def _ws_loop() -> None:
    """WebSocket trades collector (real-time, primary)."""
    print(f"[WS] Connecting to {WS_URL}...", flush=True)

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return
        channel = data.get("channel")
        if channel != "trades":
            return
        # data shape: {"channel": "trades", "data": [<trade>, ...]}
        trades = data.get("data") or []
        for t in trades:
            coin = t.get("coin", "").upper()
            if coin not in SYMBOLS:
                continue
            if _save_trade(coin, t):
                # Quick console update
                size = float(t.get("sz", 0))
                px = float(t.get("px", 0))
                side = t.get("side", "?")
                print(
                    f"  [WS] {coin:3} {side} sz={size:>10.4f} px=${px:>10,.2f}  "
                    f"notional=${size*px:>11,.0f}",
                    flush=True,
                )

    def on_open(ws):
        print("[WS] Connected, subscribing to trades...", flush=True)
        for sym in SYMBOLS:
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": sym},
            }))

    def on_error(ws, error):
        print(f"[WS] Error: {error}", flush=True)

    def on_close(ws, code, msg):
        print(f"[WS] Closed: code={code} msg={msg}", flush=True)

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
            print(f"[WS] Crashed: {e}, reconnecting in 5s...", flush=True)
        time.sleep(5)


def main() -> int:
    import threading
    print(f"HyphyLiquid - Public Trade Collector (BTC + ETH)")
    print(f"Saving to {TRADE_DIR}")
    print(f"WS: real-time trades subscription + REST safety net")
    print()

    # Start REST poller in background
    rest_thread = threading.Thread(target=_rest_loop, daemon=True)
    rest_thread.start()

    # Run WS loop in main thread
    _ws_loop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nTrade collector stopped.", flush=True)
        sys.exit(0)

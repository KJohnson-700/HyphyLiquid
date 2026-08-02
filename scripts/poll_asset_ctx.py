"""
HyphyLiquid - metaAndAssetCtxs + predictedFundings REST poller.

Polls HL info endpoint every 5 min for BTC + ETH:
  - metaAndAssetCtxs: mark price, oracle price, funding rate, OI
  - predictedFundings: predicted next funding

Saves to data/asset_ctx/{sym}_{date}.jsonl (one JSON per line per cycle)
for later OI-change / funding-delta analysis.

Run foreground (Ctrl+C to stop):
    .\\venv\\Scripts\\python.exe scripts\\poll_asset_ctx.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
# 1 min cadence — needed to compute OI delta inside the 1-5 min
# `liquidation_fade_or_follow` wait window. 5 min was too coarse.
INTERVAL_S = 60
OUT_DIR = PROJECT_ROOT / "data" / "asset_ctx"


def fetch_ctx() -> dict:
    r = requests.post(INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_predicted() -> list:
    r = requests.post(INFO_URL, json={"type": "predictedFundings"}, timeout=15)
    r.raise_for_status()
    return r.json()


def _by_coin(predicted: list) -> dict:
    """Convert predictedFundings list into {coin: {HlPerp: {...}, ...}}."""
    out = {}
    for entry in predicted:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        coin = entry[0]
        venues = entry[1]
        if not isinstance(venues, (list, tuple)):
            continue
        venue_data = {}
        for v in venues:
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                venue_data[v[0]] = v[1]
        out[coin] = venue_data
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Asset-ctx poller started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Saving to {OUT_DIR}, interval {INTERVAL_S}s")
    print()

    while True:
        cycle_ts = datetime.now(timezone.utc)
        date_str = cycle_ts.strftime("%Y-%m-%d")
        try:
            meta = fetch_ctx()
            # meta = [universe_dict, contexts_list]
            # universe_dict = {"universe": [...], "marginTables": ..., ...}
            universe_dict = meta[0] if isinstance(meta, list) and len(meta) >= 1 and isinstance(meta[0], dict) else {}
            contexts = meta[1] if isinstance(meta, list) and len(meta) >= 2 and isinstance(meta[1], list) else []
            universe = universe_dict.get("universe", []) if isinstance(universe_dict, dict) else []
            universe_by_name = {u.get("name"): u for u in universe if isinstance(u, dict) and u.get("name")}
            predicted_all = fetch_predicted()
            predicted_by_coin = _by_coin(predicted_all)
        except Exception as e:
            print(f"  fetch error: {e}", flush=True)
            time.sleep(INTERVAL_S)
            continue

        for sym in SYMBOLS:
            if sym not in universe_by_name:
                continue
            # The asset context list is parallel to the universe list (same order)
            try:
                idx = universe.index(universe_by_name[sym])
            except ValueError:
                idx = None
            ctx = contexts[idx] if idx is not None and 0 <= idx < len(contexts) else None
            record = {
                "poll_ts": cycle_ts.isoformat(),
                "symbol": sym,
                "context": ctx,
                "predicted": predicted_by_coin.get(sym, {}),
            }
            path = OUT_DIR / f"{sym.lower()}_{date_str}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            if ctx:
                mark = ctx.get("markPx", "?")
                oi = ctx.get("openInterest", "?")
                fund = ctx.get("funding", "?")
                pred_hl = predicted_by_coin.get(sym, {}).get("HlPerp", {})
                pred = pred_hl.get("fundingRate", "?") if isinstance(pred_hl, dict) else "?"
                print(
                    f"  [{cycle_ts.strftime('%H:%M:%S')}] {sym}  "
                    f"mark={mark}  oi={oi}  fund={fund}  pred={pred}",
                    flush=True,
                )
        next_wake = time.time() + INTERVAL_S
        time.sleep(max(0.0, next_wake - time.time()))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAsset-ctx poller stopped.")
        sys.exit(0)

"""One screen showing what the lanes are doing and how close they are to firing.

The daemon logs tell you what happened on the last tick. The venue tells you
what is open. Neither tells you how far a lane is from its next signal, which
is the thing worth watching -- and not knowing it is why a blocked lane went
unnoticed for four hours.

Shows: live positions and account, every order attempt with its outcome
(fills AND rejections, since rejections are what have actually happened so
far), and each lane's distance to its own trigger.

Usage:
  python3 scripts/watch_trades.py
  python3 scripts/watch_trades.py --follow          # refresh every 60s
  python3 scripts/watch_trades.py --follow --interval 300
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

TESTNET = "https://api.hyperliquid-testnet.xyz/info"
FADE_ORDERS = PROJECT_ROOT / "data" / "testnet_funding_neg_fade_orders.jsonl"
SWING_ORDERS = PROJECT_ROOT / "data" / "testnet_swing_orders.jsonl"
OPEN_POS = PROJECT_ROOT / "data" / "testnet_open_positions.json"
CALIB = PROJECT_ROOT / "data" / "swing_lane_calibration.json"


def _post(url: str, payload: dict, timeout: int = 20):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _rows(path: Path, limit: int = 6) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-limit:]


def show() -> None:
    now = datetime.now(timezone.utc)
    print("=" * 78)
    print(f"  HyphyLiquid — testnet  {now:%Y-%m-%d %H:%M:%S UTC}")
    print("=" * 78)

    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    user = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "").strip()

    # ---- account + live positions ------------------------------------
    try:
        st = _post(TESTNET, {"type": "clearinghouseState", "user": user})
        sp = _post(TESTNET, {"type": "spotClearinghouseState", "user": user})
        usdc = next((b["total"] for b in (sp.get("balances") or [])
                     if b.get("coin") == "USDC"), "n/a")
        positions = [a for a in (st.get("assetPositions") or [])
                     if float((a.get("position") or {}).get("szi", 0)) != 0]
        print(f"\nACCOUNT   spot USDC {usdc}   open positions {len(positions)}")
        for a in positions:
            p = a["position"]
            szi = float(p.get("szi", 0))
            print(f"   {p.get('coin'):8} {'LONG' if szi>0 else 'SHORT':5} "
                  f"size {abs(szi)}  entry {p.get('entryPx')}  "
                  f"uPnL {p.get('unrealizedPnl')}  liq {p.get('liquidationPx')}")
        if not positions:
            print("   (flat)")
    except Exception as e:  # noqa: BLE001
        print(f"\nACCOUNT   unreachable: {e}")

    # ---- order attempts, fills AND rejections ------------------------
    print("\nORDER ATTEMPTS  (rejections matter -- they are all that has happened so far)")
    any_row = False
    for label, path in (("fade ", FADE_ORDERS), ("swing", SWING_ORDERS)):
        for r in _rows(path):
            any_row = True
            res = r.get("result") or {}
            blocked = r.get("blocked_by")
            if blocked:
                outcome = f"BLOCKED {blocked}"
            elif res.get("filled"):
                outcome = f"FILLED oid={res.get('entry_oid')} size={res.get('size_coin')}"
            else:
                outcome = f"{res.get('status')} {str(res.get('error') or '')[:44]}"
            print(f"   {r.get('ts_utc','')[:16]}  {label} {str(r.get('symbol')):6} {outcome}")
    if not any_row:
        print("   (no attempts yet)")

    # ---- distance to the next signal ---------------------------------
    print("\nDISTANCE TO NEXT SIGNAL")
    try:
        import pandas as pd
        from strategy_search import load_hl_with_funding
        from paper_funding_neg_fade import NEG_THRESHOLD, PER_ASSET_POLICY
        for sym in sorted(PER_ASSET_POLICY):
            loaded = load_hl_with_funding(sym)
            df = loaded[0] if isinstance(loaded, tuple) else loaded
            f = float(df["funding_actual"].iloc[-1])
            prev = float(df["funding_actual"].iloc[-2])
            armed = "ARMED (prev bar not signalled)" if prev >= NEG_THRESHOLD else "prev bar already signalled"
            gap = f - NEG_THRESHOLD
            state = "SIGNAL NOW" if f < NEG_THRESHOLD else f"needs {gap:+.2e} more"
            print(f"   fade  {sym:6} funding {f:+.3e}  threshold {NEG_THRESHOLD:.1e}  {state}   {armed}")
    except Exception as e:  # noqa: BLE001
        print(f"   fade lanes: {e}")

    try:
        from swing_lane import load_panel
        cfgs = json.loads(CALIB.read_text())["survivors"] if CALIB.exists() else []
        panels = load_panel([c["symbol"] for c in cfgs]) if cfgs else {}
        for c in cfgs:
            d = panels.get(c["symbol"])
            if d is None or d.empty:
                continue
            mv = float(d.iloc[-1]["move24"])
            need = c["move_thr"]
            state = "SIGNAL NOW" if abs(mv) >= need else f"needs |move| >= {need}%"
            print(f"   swing {c['symbol']:6} 24h move {mv:+.2f}%  {state}")
    except Exception as e:  # noqa: BLE001
        print(f"   swing lane: {e}")

    print(f"\nUI: app.hyperliquid-testnet.xyz   wallet {user[:10]}...{user[-4:]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    if not args.follow:
        show()
        return 0
    while True:
        os.system("clear" if os.name != "nt" else "cls")
        show()
        print(f"refreshing every {args.interval}s — Ctrl-C to stop")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

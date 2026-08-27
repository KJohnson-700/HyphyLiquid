"""Trail the stop on open testnet positions, each tick.

The bracket submitter is fire-and-forget: entry, one stop, one target, then
nothing touches the position again. That is fine for a fixed exit and wrong for
a trailing one -- and the exit sweep showed trailing beats fixed on every
measure (HYPE 2.04 -> 2.15, both halves up). Adopting trailing exits in the
backtest WITHOUT this would recreate the exact failure that has cost this
project three times: a simulation assuming behaviour the live path does not
perform.

What it does per open position, per tick:
  1. read the position and its resting orders from the venue
  2. track the high-water mark since entry
  3. if peak * (1 - stop_pct) is ABOVE the current stop, cancel and replace
  4. never move a stop against the position -- ratchet only

The partial take-profit needs no management: half the size as a reduce-only
limit at +1R is a static order placed with the bracket.

Reads the same open-positions file both lanes write, so it covers fade and
swing without knowing which is which.

Usage:
  python3 scripts/manage_positions.py            # dry run, prints intent
  python3 scripts/manage_positions.py --arm       # actually cancel/replace
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT)); sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from paper_funding_neg_fade import (  # noqa: E402
    TESTNET_OPEN_POSITIONS_PATH, _load_live_open_positions,
    _save_live_open_positions, _testnet_guard_ok,
)

LOG = PROJECT_ROOT / "data" / "position_management.jsonl"


def _append(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception as e:  # noqa: BLE001
        print(f"  REFUSED: .env: {e}"); return 1
    ok, reason = _testnet_guard_ok()
    if not ok:
        print(f"  REFUSED: {reason}"); return 1

    open_pos = _load_live_open_positions(TESTNET_OPEN_POSITIONS_PATH)
    if not open_pos:
        print("  no open positions to manage")
        return 0

    try:
        import os
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        pk = os.getenv("HYPERLIQUID_PRIVATE_KEY", "").strip()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        url = constants.TESTNET_API_URL
        if "testnet" not in url:
            print(f"  REFUSED: {url!r} is not testnet"); return 1
        info = Info(url, skip_ws=True)
        exchange = Exchange(Account.from_key(pk), url)
        user = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"  REFUSED: clients: {e}"); return 1

    try:
        mids = {k: float(v) for k, v in info.all_mids().items()}
        resting = info.frontend_open_orders(user)
    except Exception as e:  # noqa: BLE001
        print(f"  venue read failed: {e}"); return 1

    now = datetime.now(timezone.utc)
    changed = False
    for p in open_pos:
        sym = p.get("symbol")
        px = mids.get(sym)
        if px is None:
            print(f"  {sym}: no mid, skipping"); continue
        entry = float(p.get("entry_px") or 0)
        stop_pct = float(p.get("stop_pct") or 0)
        if entry <= 0 or stop_pct <= 0:
            continue
        long_ = (p.get("side") or "long") == "long"

        # high-water mark, persisted on the position record
        peak = float(p.get("peak_px") or entry)
        peak = max(peak, px) if long_ else min(peak, px)
        p["peak_px"] = peak

        want = peak * (1 - stop_pct) if long_ else peak * (1 + stop_pct)
        cur = float(p.get("stop_px") or (entry * (1 - stop_pct) if long_
                                         else entry * (1 + stop_pct)))
        # ratchet only -- never move a stop against the position
        improves = want > cur if long_ else want < cur
        r_mult = ((px - entry) / entry / stop_pct) if long_ else ((entry - px) / entry / stop_pct)
        print(f"  {sym:6} px {px:.4f}  entry {entry:.4f}  peak {peak:.4f}  "
              f"R {r_mult:+.2f}  stop {cur:.4f} -> {want:.4f}  "
              f"{'RATCHET' if improves else 'hold'}")
        if not improves:
            continue
        if not args.arm:
            print(f"  {sym}: DRY RUN (pass --arm to cancel/replace)")
            continue

        # cancel the existing protective stop, place the tightened one
        old = [o for o in resting if o.get("coin") == sym and o.get("isTrigger")
               and o.get("reduceOnly")]
        cancelled = []
        for o in old:
            try:
                exchange.cancel(sym, int(o["oid"])); cancelled.append(o["oid"])
            except Exception as e:  # noqa: BLE001
                print(f"  {sym}: cancel {o.get('oid')} failed: {e}")
        try:
            sz = float(p.get("size_coin") or 0)
            res = exchange.order(
                sym, not long_, sz, round(want, 6),
                {"trigger": {"isMarket": True, "tpsl": "sl", "triggerPx": round(want, 6)}},
                reduce_only=True)
            p["stop_px"] = want
            changed = True
            print(f"  {sym}: stop moved to {want:.4f} (cancelled {cancelled})")
            _append({"ts": now.isoformat(), "symbol": sym, "action": "trail",
                     "from": cur, "to": want, "peak": peak, "cancelled": cancelled,
                     "response": str(res)[:300]})
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: REPLACE FAILED after cancel: {e}")
            _append({"ts": now.isoformat(), "symbol": sym, "action": "replace_failed",
                     "error": str(e)[:300], "cancelled": cancelled})

    if changed or any("peak_px" in p for p in open_pos):
        _save_live_open_positions(open_pos, TESTNET_OPEN_POSITIONS_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

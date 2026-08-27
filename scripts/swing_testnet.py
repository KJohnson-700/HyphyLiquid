"""Route the swing lane to Hyperliquid testnet as real orders.

The fade lane has had a testnet path since 2026-08-24; the swing lane never
did, so ZEC's PF 1.70 is entirely simulated. Simulation has been wrong before:
on 2026-08-26 the first real fade signal was REJECTED by the live risk manager
because venue rounding pushed risk 1.2 cents over the cap -- a failure no
simulator can produce, since it never rounds to a size tick or asks a risk
manager for permission.

Safety is not reimplemented. The guard, the OrderManager factory and the
bracket submitter are imported from paper_funding_neg_fade so both lanes share
one definition; a second copy would eventually drift, which is exactly how the
fade lane's sweep and simulator came to disagree on ETH (1.65 vs 1.04).

The position cap is portfolio-wide by construction: this writes to the same
TESTNET_OPEN_POSITIONS_PATH the fade lane uses, so three fade positions block a
swing entry the way live would.

Usage:
  python3 scripts/swing_testnet.py                 # dry run, prints intents
  python3 scripts/swing_testnet.py --arm-testnet   # submits
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from paper_funding_neg_fade import (  # noqa: E402
    MAX_OPEN_POSITIONS, TESTNET_BANKROLL_USD, TESTNET_OPEN_POSITIONS_PATH,
    TESTNET_RISK_USD, OrderManagerFactory, _append_jsonl, _load_live_open_positions,
    _save_live_open_positions, _submit_testnet_bracket, _testnet_guard_ok,
)
from swing_lane import load_panel  # noqa: E402

CALIB = PROJECT_ROOT / "data" / "swing_lane_calibration.json"
ORDERS = PROJECT_ROOT / "data" / "testnet_swing_orders.jsonl"


def current_signal(d: pd.DataFrame, move_thr: float, direction: str):
    """Fire only on the most recent CLOSED bar.

    Same staleness rule as the fade lane: a qualifying move from twelve hours
    ago is already priced, and acting on it is the stale-signal error this
    project has now made twice.
    """
    if len(d) < 30:
        return None
    last = d.iloc[-1]
    mv = last.get("move24")
    if mv is None or pd.isna(mv) or abs(mv) < move_thr:
        return None
    up = mv > 0
    long_ = up if direction == "momentum" else (not up)
    return {"ts": last["ts"], "px": float(last["close"]),
            "side": "long" if long_ else "short", "move24": float(mv)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm-testnet", action="store_true")
    args = ap.parse_args()

    print("=== TESTNET swing lane: real orders, test funds ===")
    if not CALIB.exists():
        print("  no calibration; run scripts/swing_lane.py first")
        return 1
    survivors = json.loads(CALIB.read_text())["survivors"]
    if not survivors:
        print("  no calibrated swing configs")
        return 0

    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception as e:  # noqa: BLE001
        print(f"  REFUSED: could not load .env: {e}")
        return 1
    ok, reason = _testnet_guard_ok()
    if not ok:
        print(f"  REFUSED: {reason}")
        return 1
    print(f"  guard: {reason}")

    try:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        import os
        pk = os.getenv("HYPERLIQUID_PRIVATE_KEY", "").strip()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        env_url = constants.TESTNET_API_URL
        if "testnet" not in env_url:
            print(f"  REFUSED: {env_url!r} is not a testnet endpoint")
            return 1
        info = Info(env_url, skip_ws=True)
        exchange = Exchange(Account.from_key(pk), env_url)
    except Exception as e:  # noqa: BLE001
        print(f"  REFUSED: could not build testnet clients: {e}")
        return 1
    print(f"  endpoint: {env_url}")

    open_pos = _load_live_open_positions(TESTNET_OPEN_POSITIONS_PATH)
    held = {o.get("symbol") for o in open_pos}
    print(f"  open now: {len(open_pos)}/{MAX_OPEN_POSITIONS} {sorted(held)}  "
          f"(shared with the fade lane)")

    panels = load_panel([c["symbol"] for c in survivors])
    om = OrderManagerFactory(exchange, info)

    for cfg in survivors:
        sym = cfg["symbol"]
        if sym in held:
            print(f"  {sym}: already open, skipping")
            continue
        if len(open_pos) >= MAX_OPEN_POSITIONS:
            print(f"  {sym}: BLOCKED by max_open_positions={MAX_OPEN_POSITIONS}")
            continue
        d = panels.get(sym)
        if d is None:
            print(f"  {sym}: no panel coverage")
            continue
        sig = current_signal(d, cfg["move_thr"], cfg["direction"])
        if sig is None:
            last = d.iloc[-1].get("move24")
            shown = f"{last:+.2f}%" if last is not None and not pd.isna(last) else "n/a"
            print(f"  {sym}: no signal (24h move {shown}, needs |move| > {cfg['move_thr']}%)")
            continue

        long_ = sig["side"] == "long"
        stop, tp = cfg["stop"], cfg["stop"] * 1.5
        entry = sig["px"]
        sl = entry * (1 - stop) if long_ else entry * (1 + stop)
        tgt = entry * (1 + tp) if long_ else entry * (1 - tp)
        notional = TESTNET_RISK_USD / stop
        print(f"  {sym}: {sig['side'].upper()} signal @ {sig['ts']} "
              f"(24h {sig['move24']:+.1f}%)  entry={entry:.4f} sl={sl:.4f} "
              f"tp={tgt:.4f} notional=${notional:.0f}")
        if not args.arm_testnet:
            print(f"  {sym}: DRY RUN (pass --arm-testnet to submit)")
            continue

        res = _submit_testnet_bracket(om, sym, sig["ts"], entry, sl, tgt, notional)
        try:
            from src.notify import order_event
            order_event("swing", sym, sig["side"], res,
                        {"entry_px": entry, "sl_px": sl, "tp_px": tgt})
        except Exception:
            pass
        _append_jsonl(ORDERS, {
            "ts_utc": pd.Timestamp.now(tz="UTC").isoformat(), "lane": "swing",
            "symbol": sym, "side": sig["side"],
            "intent": {"signal_ts": str(sig["ts"]), "entry_px": entry,
                       "sl_px": sl, "tp_px": tgt, "notional_usd": notional,
                       "move24": sig["move24"], "config": cfg},
            "result": res,
        })
        if res.get("filled"):
            print(f"  {sym}: FILLED oid={res.get('entry_oid')} size={res.get('size_coin')}")
            open_pos.append({
                "paper_id": f"testnet-swing-{sym}-{pd.Timestamp(sig['ts']).strftime('%Y%m%d%H%M')}",
                "symbol": sym, "side": sig["side"], "lane": "swing",
                "entry_ts": pd.Timestamp.now(tz="UTC").isoformat(),
                "entry_px": entry, "size_coin": res.get("size_coin"),
                "notional_usd": notional, "stop_pct": stop, "tp_pct": tp,
                "max_hold_h": cfg["hold_h"], "risk_usd": TESTNET_RISK_USD,
                "entry_oid": res.get("entry_oid"),
            })
            _save_live_open_positions(open_pos, TESTNET_OPEN_POSITIONS_PATH)
        else:
            print(f"  {sym}: NOT FILLED status={res.get('status')} error={res.get('error')}")

    print(f"\n  orders log: {ORDERS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

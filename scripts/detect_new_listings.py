"""Detect new Robinhood and Hyperliquid listings, and where they overlap.

Two no-auth sources, polled and diffed against the last snapshot:
  nummus.robinhood.com/currency_pairs/   425 pairs, 89 tradable
  HL info {"type":"meta"}                232 perps (+ HIP-3 via dex="xyz")

**This is a confirmation leg, not an entry signal.** Measured on the one event
we have -- CASHCAT, Robinhood-listed 2026-08-06 -- the price was already
+156.5% over the seven days BEFORE the listing, then fell 5-11% in the first
four hours after it and whipsawed an 88-point range inside 24h. By the time a
pair appears in the Robinhood API the move is largely priced; the edge, if it
exists, is upstream in social and on-chain activity.

What this file is good for:
  1. a labelled event history, so a listing strategy can eventually be
     backtested against real dates instead of one anecdote
  2. knowing immediately when a Robinhood-listed coin becomes perp-tradeable
  3. the overlap universe (75 coins currently on both venues)

Usage:
  python3 scripts/detect_new_listings.py --once
  python3 scripts/detect_new_listings.py --interval 3600
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE = PROJECT_ROOT / "data" / "listing_state.json"
EVENTS = PROJECT_ROOT / "data" / "listing_events.jsonl"
RH_URL = "https://nummus.robinhood.com/currency_pairs/"
HL_URL = "https://api.hyperliquid.xyz/info"


def _get(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post(payload: dict, timeout: int = 30):
    req = urllib.request.Request(HL_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def robinhood_tradable() -> dict:
    """code -> {symbol, tradability}. Only pairs actually tradable, not display-only."""
    out = {}
    for p in _get(RH_URL).get("results", []):
        if p.get("tradability") != "tradable" or p.get("display_only"):
            continue
        code = (p.get("asset_currency") or {}).get("code", "").upper()
        if code:
            out[code] = {"symbol": p.get("symbol"), "name": (p.get("asset_currency") or {}).get("name")}
    return out


def hyperliquid_perps() -> dict:
    """name -> {vol24, oi, lev}. Main universe plus the xyz HIP-3 board."""
    out = {}
    for dex in (None, "xyz"):
        try:
            payload = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            m, ctxs = _post(payload)
        except Exception:
            continue
        for a, c in zip(m.get("universe", []), ctxs):
            try:
                out[a["name"].upper()] = {
                    "vol24": float(c.get("dayNtlVlm") or 0),
                    "oi_usd": float(c.get("openInterest") or 0) * float(c.get("markPx") or 0),
                    "lev": a.get("maxLeverage"),
                }
            except Exception:
                continue
    return out


def _norm(code: str) -> set[str]:
    """HL prefixes some small-unit coins with k (kPEPE, kBONK)."""
    return {code, "K" + code}


def run_once(quiet: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    rh, hl = robinhood_tradable(), hyperliquid_perps()
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}

    new_rh = sorted(set(rh) - set(prev.get("rh", [])))
    new_hl = sorted(set(hl) - set(prev.get("hl", [])))
    first_run = not prev

    overlap = sorted(c for c in rh if _norm(c) & set(hl))
    events = []
    for code in new_rh:
        perp = next((k for k in _norm(code) if k in hl), None)
        events.append({"ts": now.isoformat(), "venue": "robinhood", "code": code,
                       "name": rh[code].get("name"), "hl_perp": perp,
                       "hl_vol24": hl.get(perp, {}).get("vol24") if perp else None,
                       "tradeable_now": bool(perp)})
    for name in new_hl:
        events.append({"ts": now.isoformat(), "venue": "hyperliquid", "code": name,
                       "on_robinhood": any(name in _norm(c) for c in rh),
                       "vol24": hl[name]["vol24"], "lev": hl[name]["lev"]})

    if events and not first_run:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": now.isoformat(), "rh": sorted(rh),
                               "hl": sorted(hl)}, indent=0))
    tmp.replace(STATE)

    if not quiet:
        stamp = now.strftime("%H:%M:%S")
        print(f"[{stamp}] robinhood tradable={len(rh)}  hyperliquid perps={len(hl)}  "
              f"overlap={len(overlap)}", flush=True)
        if first_run:
            print("  first run -- baseline recorded, diffs start next tick", flush=True)
        for e in events:
            if e["venue"] == "robinhood":
                where = f"HL perp {e['hl_perp']} (${e['hl_vol24']:,.0f}/24h)" if e["tradeable_now"] \
                        else "no HL perp -- not tradeable here"
                print(f"  *** NEW ROBINHOOD LISTING: {e['code']} -- {where}", flush=True)
                print(f"      note: the CASHCAT event was +156% in the 7d BEFORE listing "
                      f"and -11% in the 4h after. Confirmation, not entry.", flush=True)
            else:
                rh_flag = "also on Robinhood" if e["on_robinhood"] else "not on Robinhood"
                print(f"  *** NEW HYPERLIQUID PERP: {e['code']} "
                      f"(${e['vol24']:,.0f}/24h, {e['lev']}x, {rh_flag})", flush=True)
    return {"rh": rh, "hl": hl, "overlap": overlap, "events": events}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--show-overlap", action="store_true")
    args = ap.parse_args()

    if args.once:
        r = run_once()
        if args.show_overlap:
            print("\nRobinhood-tradable AND perp-tradeable on Hyperliquid:")
            for c in r["overlap"]:
                perp = next(k for k in _norm(c) if k in r["hl"])
                print(f"  {perp:10} ${r['hl'][perp]['vol24']:>15,.0f}/24h  "
                      f"{r['hl'][perp]['lev']}x")
        return 0

    print(f"listing detector starting, every {args.interval}s", flush=True)
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"  tick failed: {e}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

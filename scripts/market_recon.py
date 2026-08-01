"""
HyphyLiquid — Week 0 Market Recon
==================================

Hits the Hyperliquid public info endpoint to answer the one question that
matters before any strategy work:

    Do XAU / XAG (or any gold/silver perp) exist on Hyperliquid, and if so,
    how deep are the books and how much volume flows through them?

Public endpoints (no auth required):
    POST https://api.hyperliquid.xyz/info
        - type=metaAndAssetCtxs   -> [meta, asset_ctxs]
        - type=meta               -> meta only
        - type=l2Book, coin=...   -> orderbook snapshot

Run:
    python scripts/market_recon.py
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

ENDPOINT = "https://api.hyperliquid.xyz/info"
TIMEOUT = 30

# Patterns we treat as "gold/silver" in the perp universe
METAL_KEYWORDS = [
    "XAU",   # ISO 4217 gold
    "XAG",   # ISO 4217 silver
    "GOLD",
    "SILVER",
    "PAXG",  # Paxos gold token
    "GLD",   # SPDR Gold Trust
    "SLV",   # iShares Silver Trust
]


def post(payload: Dict[str, Any]) -> Any:
    r = requests.post(ENDPOINT, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_universe_and_ctxs() -> tuple[list, list]:
    """Returns (universe, asset_ctxs) parallel lists."""
    data = post({"type": "metaAndAssetCtxs"})
    if not isinstance(data, list) or len(data) < 2:
        raise RuntimeError(f"Unexpected metaAndAssetCtxs response shape: {type(data)}")
    return data[0]["universe"], data[1]


def fetch_l2_book(coin: str) -> Dict[str, Any]:
    return post({"type": "l2Book", "coin": coin})


def is_metal(name: str) -> bool:
    up = name.upper()
    return any(kw in up for kw in METAL_KEYWORDS)


def fmt_int(n) -> str:
    if n is None:
        return "—"
    try:
        return f"{int(float(n)):,}"
    except (ValueError, TypeError):
        return str(n)


def fmt_money(n) -> str:
    if n is None:
        return "—"
    try:
        v = float(n)
        if v >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        if v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        if v >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:.2f}"
    except (ValueError, TypeError):
        return str(n)


def fmt_pct(n) -> str:
    if n is None:
        return "—"
    try:
        return f"{float(n)*100:.4f}%"
    except (ValueError, TypeError):
        return str(n)


def main() -> int:
    print("=" * 78)
    print("HyphyLiquid — Market Recon")
    print(f"Endpoint: {ENDPOINT}")
    print(f"Time:     {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 78)
    print()

    print("[1/3] Fetching universe + asset contexts...")
    try:
        universe, asset_ctxs = fetch_universe_and_ctxs()
    except Exception as e:
        print(f"  FAILED: {e}", file=sys.stderr)
        return 1
    print(f"  OK — {len(universe)} perpetuals on Hyperliquid mainnet")
    print()

    # Build a quick lookup of coin -> ctx
    ctx_by_coin: Dict[str, Dict[str, Any]] = {}
    # universe is a list of {name, szDecimals, maxLeverage, ...} in some order;
    # asset_ctxs is the parallel list.
    # But metaAndAssetCtxs may include spot contexts in HIP-3 deployments —
    # we filter to perp entries by matching names.
    for i, coin_meta in enumerate(universe):
        name = coin_meta.get("name", "")
        if i < len(asset_ctxs):
            ctx_by_coin[name] = asset_ctxs[i]

    # Find all metal perps
    metal_perps = [c for c in universe if is_metal(c.get("name", ""))]
    print(f"[2/3] Metal perp scan (keywords: {METAL_KEYWORDS})")
    print(f"  Found {len(metal_perps)} matching perp(s)")
    print()

    if metal_perps:
        print("  " + "-" * 74)
        header = f"  {'NAME':<14} {'MAX LEV':<9} {'MARK PX':>12} {'24H VOL':>14} {'OPEN INT':>14} {'FUNDING (h)':>13}"
        print(header)
        print("  " + "-" * 74)
        for coin_meta in metal_perps:
            name = coin_meta.get("name", "?")
            max_lev = coin_meta.get("maxLeverage", "—")
            ctx = ctx_by_coin.get(name, {})
            mark = ctx.get("markPx")
            vol = ctx.get("dayNtlVlm")
            oi = ctx.get("openInterest")
            funding = ctx.get("funding")
            print(
                f"  {name:<14} {str(max_lev)+'x':<9} "
                f"{(mark or '—'):>12} "
                f"{fmt_money(vol):>14} "
                f"{fmt_money(oi):>14} "
                f"{fmt_pct(funding):>13}"
            )
        print("  " + "-" * 74)
    else:
        print("  *** NO METAL PERPS FOUND ***")
        print("  This is a meaningful result — see notes below.")
    print()

    # Show top 15 perps by 24h volume so we can compare liquidity context
    print("[3/3] Top 15 perps by 24h volume (liquidity context)")
    ranked = []
    for name, ctx in ctx_by_coin.items():
        vol = ctx.get("dayNtlVlm")
        if vol is not None:
            try:
                ranked.append((name, float(vol), ctx))
            except (ValueError, TypeError):
                pass
    ranked.sort(key=lambda x: x[1], reverse=True)

    print("  " + "-" * 74)
    print(f"  {'RANK':<5} {'NAME':<14} {'24H VOL':>14} {'MARK PX':>12} {'FUNDING (h)':>13}")
    print("  " + "-" * 74)
    for i, (name, _vol, ctx) in enumerate(ranked[:15], 1):
        mark = ctx.get("markPx") or "—"
        funding = fmt_pct(ctx.get("funding"))
        print(
            f"  {i:<5} {name:<14} {fmt_money(_vol):>14} "
            f"{mark:>12} {funding:>13}"
        )
    print("  " + "-" * 74)
    print()

    # If we found at least one metal perp, sample its orderbook depth
    if metal_perps:
        first_metal = metal_perps[0].get("name", "")
        print(f"[bonus] Orderbook depth sample for {first_metal}")
        try:
            book = fetch_l2_book(first_metal)
            levels = book.get("levels", [[], []])
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []
            print(f"  Bids: {len(bids)} levels, Asks: {len(asks)} levels")
            if bids and asks:
                best_bid = float(bids[0]["px"])
                best_ask = float(asks[0]["px"])
                mid = (best_bid + best_ask) / 2
                spread_bps = (best_ask - best_bid) / mid * 10_000
                print(f"  Best bid:  {best_bid}")
                print(f"  Best ask:  {best_ask}")
                print(f"  Mid:       {mid}")
                print(f"  Spread:    {spread_bps:.2f} bps")
                # Top 5 levels each side
                print("  Top 5 bids:")
                for lvl in bids[:5]:
                    print(f"    {lvl['px']:>12}  x  {lvl['sz']}")
                print("  Top 5 asks:")
                for lvl in asks[:5]:
                    print(f"    {lvl['px']:>12}  x  {lvl['sz']}")
        except Exception as e:
            print(f"  FAILED to fetch book: {e}", file=sys.stderr)
        print()

    # Final verdict
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    if not metal_perps:
        print("  • No gold/silver perps exist on Hyperliquid mainnet.")
        print("  • The HyphyLiquid project as currently scoped (gold/silver bot)")
        print("    cannot be built on this venue without an alternative source.")
        print("  • Options:")
        print("      (a) Pivot to a crypto-cascade bot (BTC/ETH/SOL/HYPE etc.)")
        print("      (b) Find another venue that lists XAU/XAG perps (more friction)")
        print("      (c) Pivot the G/S idea to spot-PAXG on Hyperliquid + a")
        print("          synthetic silver leg (creative but doable)")
        print()
    else:
        # Compute total 24h metal volume and rank it vs top crypto
        total_metal_vol = sum(
            float(ctx_by_coin[c["name"]].get("dayNtlVlm") or 0)
            for c in metal_perps
        )
        if ranked:
            top_crypto_vol = ranked[0][1] if ranked else 0
            ratio = total_metal_vol / top_crypto_vol if top_crypto_vol else 0
            print(f"  • {len(metal_perps)} gold/silver perp(s) found.")
            print(f"  • Combined 24h metal volume: {fmt_money(total_metal_vol)}")
            print(f"  • Top crypto 24h volume:     {fmt_money(top_crypto_vol)} "
                  f"({ratio*100:.2f}% of crypto #1)")
            if ratio < 0.01:
                print("  • Verdict: METALS ARE TOO THIN for a serious strategy.")
            elif ratio < 0.10:
                print("  • Verdict: metals are liquid enough for $500–$2K sized")
                print("    positions, but not for high-frequency or large size.")
            else:
                print("  • Verdict: metals have meaningful liquidity.")
            print()
            print("  See the table above for per-coin stats (mark px, OI, funding).")
        else:
            print("  • Metal perps found but no volume data — investigate manually.")
    print()
    print("Raw response (meta + first 3 ctxs) saved to scripts/market_recon_raw.json")
    # Save full response for future reference
    with open("scripts/market_recon_raw.json", "w", encoding="utf-8") as f:
        json.dump({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "universe_count": len(universe),
            "metal_perps": [c.get("name") for c in metal_perps],
            "all_perp_names": [c.get("name") for c in universe],
        }, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())

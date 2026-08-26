"""Track Robinhood Chain's growth arc, and watch for its tokens reaching Hyperliquid.

The thesis this exists to test: a NEW chain has an arc -- launch, mania,
saturation, decay -- and the tokens on it ride that arc. Robinhood Chain
launched 2026-07-01, so nothing about it can be backtested; the population is
two months old. This starts the record.

Why the earlier memecoin backtest missed the point: it ran on PEPE, SHIB, BONK,
WIF, POPCAT -- established coins whose hype cycles ended years ago. That says
nothing about a chain still in its first growth phase.

What is captured, all free and no-auth:

  chain TVL + growth rate (DefiLlama)
      TVL is still at all-time high, but the growth rate has collapsed:
      +244% -> +88% -> +40% -> +18% -> +21% -> +15% -> +9% -> +6.3% weekly.
      The second derivative turned over well before TVL will. That deceleration
      is the leading indicator, not the level.

  protocol launch velocity
      New protocols per week on the chain -- the closest measurable proxy for
      "how many things are launching", which is the mania gauge. Eight appeared
      in the week to 2026-08-26.

  launchpads specifically (StonkBrokers, token.select, ...)
      A launchpad's TVL is a direct read on how much new-token activity the
      chain is supporting.

  bridge: does a watched token have a Hyperliquid perp yet
      Nothing on Robinhood Chain is tradeable here until Hyperliquid lists a
      perp. CASHCAT is currently the only one that has.

No claim is made that any of this predicts price. Weekly TVL change against
CASHCAT correlates -0.42 (and -0.52 with TVL leading), but that is seven weekly
observations -- a story, not a finding. A monthly correlation of -0.68 on six
points already fooled us once today; it inverted at trade level.

Usage:
  python3 scripts/collect_chain_metrics.py --once
  python3 scripts/collect_chain_metrics.py --interval 21600
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "data" / "chain_metrics"
STATE = PROJECT_ROOT / "data" / "chain_protocol_state.json"
CHAIN = "Robinhood Chain"
CHAIN_SLUG = "Robinhood%20Chain"

# Tokens/protocols to watch for a Hyperliquid listing. Add freely -- a name
# here that never lists costs nothing.
WATCH = ["CASHCAT", "STONK", "STONKBROKERS", "STONX", "DOGINHOOD", "ARROW",
         "BYCOCKET", "FABLES", "DEEPSTATE"]


def _get(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _hl(payload: dict, timeout: int = 30):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def chain_tvl() -> dict:
    """Current TVL plus growth over 1/7/30d and the change in growth rate.

    The acceleration term is the point. TVL rising says the chain is alive;
    growth decelerating says the mania phase is ending, and that turns first.
    """
    hist = _get(f"https://api.llama.fi/v2/historicalChainTvl/{CHAIN_SLUG}")
    if not hist:
        return {}
    series = [(int(p["date"]), float(p["tvl"])) for p in hist]
    series.sort()
    now_tvl = series[-1][1]

    def ago(days: int):
        target = series[-1][0] - days * 86400
        prior = [v for t, v in series if t <= target]
        return prior[-1] if prior else None

    def growth(days: int):
        p = ago(days)
        return (now_tvl / p - 1) * 100 if p else None

    g7, g30 = growth(7), growth(30)
    # growth of the previous 7d window, to see whether growth is slowing
    prev7 = None
    p7, p14 = ago(7), ago(14)
    if p7 and p14:
        prev7 = (p7 / p14 - 1) * 100
    return {
        "tvl_usd": now_tvl,
        "peak_usd": max(v for _, v in series),
        "pct_off_peak": (now_tvl / max(v for _, v in series) - 1) * 100,
        "growth_1d": growth(1), "growth_7d": g7, "growth_30d": g30,
        "growth_7d_prev": prev7,
        "growth_accel": (g7 - prev7) if (g7 is not None and prev7 is not None) else None,
        "days_of_history": len(series),
    }


def protocols() -> list[dict]:
    out = []
    for p in _get("https://api.llama.fi/protocols"):
        if CHAIN in (p.get("chains") or []):
            out.append({"name": p.get("name"), "category": p.get("category"),
                        "tvl": p.get("tvl"), "change_1d": p.get("change_1d"),
                        "change_7d": p.get("change_7d"),
                        "listed_at": p.get("listedAt"), "url": p.get("url")})
    return out


def hl_universe() -> dict:
    out = {}
    for dex in (None, "xyz"):
        try:
            payload = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            m, ctxs = _hl(payload)
        except Exception:
            continue
        for a, c in zip(m.get("universe", []), ctxs):
            try:
                out[a["name"].upper()] = float(c.get("dayNtlVlm") or 0)
            except Exception:
                continue
    return out


def run_once(quiet: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    tvl = chain_tvl()
    protos = protocols()
    hl = hl_universe()

    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}
    prev_names = set(prev.get("protocols", []))
    new_protos = sorted({p["name"] for p in protos} - prev_names) if prev_names else []

    # launch velocity from DefiLlama's own listedAt stamps
    cutoffs = {"7d": 7 * 86400, "30d": 30 * 86400}
    now_s = int(now.timestamp())
    velocity = {k: sum(1 for p in protos if p.get("listed_at")
                       and now_s - p["listed_at"] <= v) for k, v in cutoffs.items()}

    pads = sorted([p for p in protos if p.get("category") == "Launchpad"],
                  key=lambda p: -(p.get("tvl") or 0))

    # which watched names are perp-tradeable yet
    bridge = []
    for w in WATCH:
        hit = next((k for k in hl if k == w or k == "K" + w), None)
        bridge.append({"name": w, "hl_perp": hit, "hl_vol24": hl.get(hit) if hit else None})

    rec = {"ts": now.isoformat(), "chain": CHAIN, "tvl": tvl,
           "n_protocols": len(protos), "launch_velocity": velocity,
           "new_protocols": new_protos,
           "launchpads": [{k: p[k] for k in ("name", "tvl", "change_7d")} for p in pads[:6]],
           "bridge": bridge}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"ts": now.isoformat(),
                                 "protocols": sorted(p["name"] for p in protos)}))

    if not quiet:
        t = tvl
        print(f"[{now:%H:%M:%S}] {CHAIN}: TVL ${t.get('tvl_usd',0)/1e6:,.0f}M "
              f"({t.get('pct_off_peak',0):+.1f}% off peak)  "
              f"7d {t.get('growth_7d',0):+.1f}%  30d {t.get('growth_30d',0):+.1f}%",
              flush=True)
        acc = t.get("growth_accel")
        if acc is not None:
            trend = "DECELERATING" if acc < 0 else "accelerating"
            print(f"           growth {t.get('growth_7d_prev',0):+.1f}% -> "
                  f"{t.get('growth_7d',0):+.1f}%  ({trend})", flush=True)
        print(f"           protocols {len(protos)}   new listings 7d={velocity['7d']} "
              f"30d={velocity['30d']}", flush=True)
        if pads:
            p = pads[0]
            print(f"           top launchpad {p['name']} ${(p.get('tvl') or 0)/1e6:.2f}M "
                  f"({(p.get('change_7d') or 0):+.1f}% 7d)", flush=True)
        for b in bridge:
            if b["hl_perp"]:
                print(f"           {b['name']} IS perp-tradeable on HL as {b['hl_perp']} "
                      f"(${b['hl_vol24']:,.0f}/24h)", flush=True)
        if new_protos:
            print(f"  *** NEW ON CHAIN: {', '.join(new_protos[:10])}", flush=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=21600)  # 4x/day; TVL is daily
    args = ap.parse_args()
    if args.once:
        run_once()
        return 0
    print(f"chain metrics collector starting, every {args.interval}s", flush=True)
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"  tick failed: {e}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

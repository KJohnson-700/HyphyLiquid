"""Profile every Hyperliquid perp listing: when, and what the token looked like.

Purpose: build the comparison set for spotting the next listing worth trading.
The Robinhood Chain -> Hyperliquid pipeline has exactly one completed case
(CASHCAT), which is not enough to characterise anything on its own. Widening to
every recent HL listing gives a baseline to measure a single new case against.

For each perp it records the first candle date (the listing), and the price
behaviour after it at 4h/24h/72h/7d/30d plus the maximum favourable and adverse
excursion. MFE/MAE is the number that matters: measured on the three listings
with volume, CHIP ran +133% on a -1.7% MAE (ratio 77) while CASHCAT bled -80.7%
(ratio 0.39). Same setup, opposite outcome -- so the entry rule is not the
interesting part, the dispersion is.

Writes data/hl_listing_profiles.json, cached so reruns are cheap.

Usage:
  python3 scripts/profile_hl_listings.py                 # scan + report
  python3 scripts/profile_hl_listings.py --max-age 240   # only recent listings
  python3 scripts/profile_hl_listings.py --report-only   # from cache, no network
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT = PROJECT_ROOT / "data" / "hl_listing_profiles.json"
INFO = "https://api.hyperliquid.xyz/info"
SLEEP = 0.30


def _post(payload: dict, tries: int = 5):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(INFO, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3.0 * (a + 1))
    raise RuntimeError(f"{payload.get('type')} failed: {last}")


def universe() -> dict:
    out = {}
    for dex in (None, "xyz"):
        payload = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        try:
            m, ctxs = _post(payload)
        except Exception:
            continue
        for a, c in zip(m.get("universe", []), ctxs):
            try:
                out[a["name"]] = {
                    "vol24": float(c.get("dayNtlVlm") or 0),
                    "oi_usd": float(c.get("openInterest") or 0) * float(c.get("markPx") or 0),
                    "lev": a.get("maxLeverage"), "sz_dec": a.get("szDecimals"),
                    "funding": float(c.get("funding") or 0),
                }
            except Exception:
                continue
        time.sleep(SLEEP)
    return out


def profile(coin: str) -> dict | None:
    """First candle + post-listing excursions. None if history is unusable."""
    start = int((datetime.now(timezone.utc) - timedelta(days=730)).timestamp() * 1000)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        daily = _post({"type": "candleSnapshot",
                       "req": {"coin": coin, "interval": "1d",
                               "startTime": start, "endTime": end}})
    except Exception:
        return None
    if not daily:
        return None
    first_ms = int(daily[0]["t"])
    listed = datetime.fromtimestamp(first_ms / 1000, timezone.utc)
    age_days = (datetime.now(timezone.utc) - listed).days
    return {"coin": coin, "listed": listed.strftime("%Y-%m-%d"),
            "age_days": age_days, "n_daily_bars": len(daily), "first_ms": first_ms}


def excursions(coin: str, first_ms: int) -> dict:
    """Hourly behaviour after listing. Entry is the close of the first hour --
    the first price a taker could realistically have got."""
    end = first_ms + 40 * 86400 * 1000
    try:
        h = _post({"type": "candleSnapshot",
                   "req": {"coin": coin, "interval": "1h",
                           "startTime": first_ms, "endTime": end}})
    except Exception:
        return {}
    if len(h) < 6:
        return {}
    px = [float(c["c"]) for c in h]
    hi = [float(c["h"]) for c in h]
    lo = [float(c["l"]) for c in h]
    e = px[0]
    if e <= 0:
        return {}
    out = {}
    for label, n in (("r4h", 4), ("r24h", 24), ("r72h", 72), ("r7d", 168), ("r30d", 720)):
        if len(px) > n:
            out[label] = round((px[n] / e - 1) * 100, 2)
    w = slice(1, min(169, len(px)))          # first 7 days
    mfe = round((max(hi[w]) / e - 1) * 100, 2)
    mae = round((min(lo[w]) / e - 1) * 100, 2)
    out["mfe_7d"] = mfe
    out["mae_7d"] = mae
    out["mfe_mae"] = round(abs(mfe / mae), 2) if mae else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", type=int, default=400,
                    help="only profile perps listed within this many days")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    if args.report_only and OUT.exists():
        data = json.loads(OUT.read_text())
    else:
        uni = universe()
        print(f"scanning {len(uni)} perps for listing dates...", flush=True)
        rows = []
        for i, coin in enumerate(uni, 1):
            p = profile(coin)
            time.sleep(SLEEP)
            if not p:
                continue
            p.update({k: uni[coin][k] for k in ("vol24", "oi_usd", "lev", "funding")})
            if p["age_days"] <= args.max_age:
                p.update(excursions(coin, p["first_ms"]))
                time.sleep(SLEEP)
            rows.append(p)
            if i % 40 == 0:
                print(f"  {i}/{len(uni)}...", flush=True)
        data = {"generated": datetime.now(timezone.utc).isoformat(), "listings": rows}
        OUT.write_text(json.dumps(data, indent=2))
        print(f"wrote {OUT}")

    rows = data["listings"]
    recent = sorted([r for r in rows if r["age_days"] <= args.max_age and "mfe_mae" in r],
                    key=lambda r: r["age_days"])
    print(f"\n{len(rows)} perps profiled; {len(recent)} listed within {args.max_age}d "
          f"with usable post-listing history\n")
    print(f"{'coin':13}{'listed':>12}{'age':>5}{'vol24 $':>14}"
          f"{'+24h':>8}{'+7d':>8}{'MFE':>8}{'MAE':>8}{'M/M':>7}")
    for r in recent[:40]:
        print(f"{r['coin']:13}{r['listed']:>12}{r['age_days']:>5}{r['vol24']:>14,.0f}"
              f"{r.get('r24h', float('nan')):>+7.1f}%{r.get('r7d', float('nan')):>+7.1f}%"
              f"{r.get('mfe_7d', float('nan')):>+7.1f}%{r.get('mae_7d', float('nan')):>+7.1f}%"
              f"{r.get('mfe_mae') or 0:>7.1f}")

    good = [r for r in recent if (r.get("mfe_mae") or 0) >= 2]
    bad = [r for r in recent if (r.get("mfe_mae") or 0) < 1]
    print(f"\nMFE/MAE >= 2 (favourable): {len(good)}/{len(recent)}   < 1 (adverse): {len(bad)}")
    if recent:
        med = sorted(r.get("r7d", 0) for r in recent)[len(recent) // 2]
        print(f"median 7d return after listing: {med:+.1f}%")
        pos = sum(1 for r in recent if (r.get("r7d") or 0) > 0)
        print(f"positive at 7d: {pos}/{len(recent)} ({pos/len(recent):.0%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

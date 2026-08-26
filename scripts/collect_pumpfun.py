"""Track pump.fun's top tokens and which of them reach a Hyperliquid perp.

pump.fun graduates a large share of the memecoins that eventually get listed on
Hyperliquid, which makes it the highest-sample version of the "new token
pipeline" thesis. Robinhood Chain has one completed case (CASHCAT); pump.fun
has years of them, so it is the only pipeline with enough history to test
whether pre-listing metrics predict post-listing behaviour.

frontend-api-v3.pump.fun/coins is public and no-auth. Useful fields:

  usd_market_cap                  current size
  ath_market_cap + _timestamp     peak and when -- lets us measure how far past
                                  its peak a token was when Hyperliquid listed it
  reply_count                     native social engagement, no scraping needed
  created_timestamp               age, so "how fast did it get there" is knowable
  complete                        graduated off the bonding curve
  twitter                         handle, for a future social-velocity leg

The point of collecting rather than backtesting: pump.fun's API returns the
CURRENT state, not history. ath_market_cap_timestamp gives one historical
anchor, but market cap at the moment Hyperliquid listed a token is not
recoverable after the fact. Snapshotting now makes future listings measurable.

Writes data/pumpfun_snapshots/{YYYY-MM}.jsonl and flags any tracked token that
gains a Hyperliquid perp.

Usage:
  python3 scripts/collect_pumpfun.py --once
  python3 scripts/collect_pumpfun.py --once --pages 10
  python3 scripts/collect_pumpfun.py --interval 21600
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "data" / "pumpfun_snapshots"
STATE = PROJECT_ROOT / "data" / "pumpfun_state.json"
PF = "https://frontend-api-v3.pump.fun/coins"
INFO = "https://api.hyperliquid.xyz/info"
PAGE = 50
SLEEP = 0.5


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _hl(payload: dict, tries: int = 4):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(INFO, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.5 * (a + 1))
    raise RuntimeError(f"HL {payload.get('type')} failed: {last}")


def top_coins(pages: int = 6) -> list[dict]:
    out, seen = [], set()
    for p in range(pages):
        try:
            batch = _get(f"{PF}?offset={p*PAGE}&limit={PAGE}&sort=market_cap"
                         f"&order=DESC&includeNsfw=false")
        except Exception:
            break
        if not batch:
            break
        for c in batch:
            mint = c.get("mint")
            if not mint or mint in seen:
                continue
            seen.add(mint)
            out.append(c)
        time.sleep(SLEEP)
    return out


def hl_universe() -> dict:
    out = {}
    for dex in (None, "xyz"):
        payload = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        try:
            m, ctxs = _hl(payload)
        except Exception:
            continue
        for a, c in zip(m.get("universe", []), ctxs):
            try:
                out[a["name"].upper()] = float(c.get("dayNtlVlm") or 0)
            except Exception:
                continue
    return out


def _hl_match(symbol: str, hl: dict) -> str | None:
    """HL prefixes some small-unit coins with k."""
    s = (symbol or "").upper()
    for cand in (s, "K" + s):
        if cand in hl:
            return cand
    return None


def run_once(pages: int, quiet: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    coins = top_coins(pages)
    hl = hl_universe()

    rows = []
    for c in coins:
        sym = c.get("symbol")
        perp = _hl_match(sym, hl)
        created = c.get("created_timestamp")
        ath_ts = c.get("ath_market_cap_timestamp")
        mc = c.get("usd_market_cap") or c.get("market_cap_usd") or 0
        ath = c.get("ath_market_cap") or 0
        rows.append({
            "ts": now.isoformat(), "mint": c.get("mint"), "symbol": sym,
            "name": c.get("name"), "mcap_usd": mc, "ath_mcap": ath,
            "pct_off_ath": round((mc / ath - 1) * 100, 2) if ath else None,
            "ath_at": ath_ts, "created_at": created,
            "age_days": round((now.timestamp() * 1000 - created) / 86400000, 1) if created else None,
            "replies": c.get("reply_count"), "complete": c.get("complete"),
            "twitter": c.get("twitter"),
            "hl_perp": perp, "hl_vol24": hl.get(perp) if perp else None,
        })

    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}
    prev_perps = set(prev.get("with_perp", []))
    with_perp = {r["symbol"] for r in rows if r["hl_perp"]}
    newly = sorted(with_perp - prev_perps) if prev_perps else []

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"ts": now.isoformat(),
                                 "with_perp": sorted(with_perp)}))

    if not quiet:
        print(f"[{now:%H:%M:%S}] pump.fun: {len(rows)} coins tracked, "
              f"{len(with_perp)} have a Hyperliquid perp", flush=True)
        hits = sorted([r for r in rows if r["hl_perp"]],
                      key=lambda r: -(r["hl_vol24"] or 0))
        for r in hits[:10]:
            print(f"    {r['symbol']:14} mcap ${r['mcap_usd']:>13,.0f}  "
                  f"off-ATH {r['pct_off_ath'] if r['pct_off_ath'] is not None else 0:>+7.1f}%  "
                  f"replies {r['replies'] or 0:>6}  HL {r['hl_perp']} "
                  f"${r['hl_vol24']:,.0f}/24h", flush=True)
        for s in newly:
            print(f"  *** {s} GAINED A HYPERLIQUID PERP since last snapshot", flush=True)
    return {"rows": rows, "with_perp": sorted(with_perp), "newly": newly}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--pages", type=int, default=6, help=f"{PAGE} coins per page")
    ap.add_argument("--interval", type=int, default=21600)
    args = ap.parse_args()
    if args.once:
        run_once(args.pages)
        return 0
    print(f"pump.fun collector starting, {args.pages} pages every {args.interval}s", flush=True)
    while True:
        try:
            run_once(args.pages)
        except Exception as e:  # noqa: BLE001
            print(f"  tick failed: {e}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

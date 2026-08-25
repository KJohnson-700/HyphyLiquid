"""Snapshot Hyperliquid whale positioning — who is on which side, with how much.

Every other lane in this project reads the same public price/liquidation feed
every other bot reads, and measured out at ~0.03% of edge against 0.09% of
costs. This reads something different: the actual positions of accounts that
are demonstrably profitable, which Hyperliquid exposes and most venues do not.

Two public sources, both no-auth:
  stats-data.hyperliquid.xyz/Mainnet/leaderboard  -> 43k accounts, PnL/ROI/value
  info {"type":"clearinghouseState","user":addr}  -> that address's live positions

Writes one row per (snapshot, coin) to data/whale_snapshots/{date}.jsonl:
  ts, coin, long_usd, short_usd, net_usd, skew, n_long, n_short, mid, filters

**The mid price is recorded with every snapshot on purpose.** The open question
is whether whale skew leads price; without the price at observation time the
history cannot answer it later, and no amount of re-fetching recovers a mid
from three weeks ago.

Per-whale detail goes to data/whale_positions/{date}.jsonl so the cohort can be
re-cut later (top 10 vs top 100, by ROI vs by size) without re-collecting --
aggregates thrown away now cannot be recovered.

Usage:
  python3 scripts/collect_whale_positions.py --once
  python3 scripts/collect_whale_positions.py --interval 3600
  python3 scripts/collect_whale_positions.py --once --top 100 --rank-by volume
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = PROJECT_ROOT / "data" / "whale_snapshots"
POS_DIR = PROJECT_ROOT / "data" / "whale_positions"
LEADERBOARD_CACHE = PROJECT_ROOT / "data" / "whale_leaderboard.json"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

# The leaderboard is ~36 MB and moves slowly; the positions are what change.
LEADERBOARD_TTL_S = 6 * 3600
REQUEST_SLEEP_S = 0.08          # ~12 req/s, well under the public limit
MAX_RETRIES = 3


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _post(payload: dict, retries: int = MAX_RETRIES):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                INFO_URL, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            # back off on rate limits rather than hammering
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"info {payload.get('type')} failed: {last}")


def fetch_leaderboard(max_age_s: int = LEADERBOARD_TTL_S) -> list[dict]:
    """Leaderboard rows, cached on disk. 36 MB is not worth re-pulling hourly."""
    if LEADERBOARD_CACHE.exists():
        age = time.time() - LEADERBOARD_CACHE.stat().st_mtime
        if age < max_age_s:
            try:
                return json.loads(LEADERBOARD_CACHE.read_text())["leaderboardRows"]
            except Exception:
                pass  # fall through and refetch
    req = urllib.request.Request(LEADERBOARD_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    LEADERBOARD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEADERBOARD_CACHE.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(LEADERBOARD_CACHE)
    return json.loads(raw)["leaderboardRows"]


def _window(row: dict, name: str) -> tuple[float, float]:
    for k, v in row.get("windowPerformances", []):
        if k == name:
            try:
                return float(v.get("pnl", 0)), float(v.get("roi", 0))
            except Exception:
                return 0.0, 0.0
    return 0.0, 0.0


def _window_field(row: dict, name: str, field: str) -> float:
    for k, v in row.get("windowPerformances", []):
        if k == name:
            try:
                return float(v.get(field, 0))
            except Exception:
                return 0.0
    return 0.0


def select_whales(rows: list[dict], *, top: int, min_value: float,
                  window: str, require_profit: bool,
                  rank_by: str = "roi") -> list[dict]:
    """Profitable accounts above a size floor, ranked by `rank_by`.

    Ranking choice matters more than anything else here. Measured on the live
    leaderboard, of the top 30 by each key, the number actually holding a
    position was:

        accountValue   3/30   (31 positions)  <- vaults and idle treasuries
        month pnl      3/30   ( 4 positions)  <- same accounts, same problem
        month vlm      9/30   (131 positions) <- fewer holders, huge books
        month roi     21/30   ( 67 positions) <- best hit rate

    Ranking by size selects capital, not traders. Default is roi among accounts
    already filtered to >= min_value and profitable, which keeps the cohort both
    sized and active. Use volume when you want the largest books rather than the
    broadest cohort.
    """
    keys = {
        "roi": lambda d: d["roi"],
        "pnl": lambda d: d["pnl"],
        "volume": lambda d: d["volume"],
        "value": lambda d: d["account_value"],
    }
    if rank_by not in keys:
        raise ValueError(f"rank_by must be one of {sorted(keys)}, got {rank_by!r}")

    out = []
    for r in rows:
        try:
            av = float(r.get("accountValue", 0))
        except Exception:
            continue
        if av < min_value:
            continue
        pnl, roi = _window(r, window)
        if require_profit and pnl <= 0:
            continue
        out.append({"addr": r.get("ethAddress", ""), "name": r.get("displayName") or "",
                    "account_value": av, "pnl": pnl, "roi": roi,
                    "volume": _window_field(r, window, "vlm")})
    out.sort(key=keys[rank_by], reverse=True)
    return out[:top]


def fetch_mids() -> dict:
    try:
        return {k: float(v) for k, v in _post({"type": "allMids"}).items()}
    except Exception:
        return {}


def collect(top: int, min_value: float, window: str, require_profit: bool,
            rank_by: str = "roi") -> dict:
    rows = fetch_leaderboard()
    whales = select_whales(rows, top=top, min_value=min_value,
                           window=window, require_profit=require_profit,
                           rank_by=rank_by)
    mids = fetch_mids()
    now = datetime.now(timezone.utc)

    longs, shorts = defaultdict(float), defaultdict(float)
    n_long, n_short = defaultdict(int), defaultdict(int)
    per_whale, read, failed = [], 0, 0

    for w in whales:
        try:
            st = _post({"type": "clearinghouseState", "user": w["addr"]})
        except Exception:
            failed += 1
            continue
        read += 1
        for ap in (st.get("assetPositions") or []):
            p = (ap or {}).get("position") or {}
            coin = p.get("coin")
            try:
                szi = float(p.get("szi", 0))
                nv = float(p.get("positionValue", 0))
                entry = float(p.get("entryPx") or 0)
            except Exception:
                continue
            if not coin or szi == 0:
                continue
            side = "long" if szi > 0 else "short"
            (longs if szi > 0 else shorts)[coin] += nv
            (n_long if szi > 0 else n_short)[coin] += 1
            per_whale.append({
                "ts": now.isoformat(), "addr": w["addr"], "name": w["name"],
                "account_value": w["account_value"], "roi": w["roi"],
                "coin": coin, "side": side, "notional_usd": nv,
                "size": szi, "entry_px": entry,
            })
        time.sleep(REQUEST_SLEEP_S)

    filters = {"top": top, "min_value": min_value, "window": window,
               "require_profit": require_profit, "rank_by": rank_by,
               "eligible": len(whales), "read": read, "failed": failed}

    snapshot = []
    for coin in set(list(longs) + list(shorts)):
        tot = longs[coin] + shorts[coin]
        snapshot.append({
            "ts": now.isoformat(), "coin": coin,
            "long_usd": round(longs[coin], 2), "short_usd": round(shorts[coin], 2),
            "net_usd": round(longs[coin] - shorts[coin], 2),
            "skew": round((longs[coin] - shorts[coin]) / tot, 4) if tot else 0.0,
            "n_long": n_long[coin], "n_short": n_short[coin],
            "mid": mids.get(coin),          # price at observation — see module docstring
            "filters": filters,
        })
    snapshot.sort(key=lambda r: -(r["long_usd"] + r["short_usd"]))
    return {"snapshot": snapshot, "per_whale": per_whale, "filters": filters, "now": now}


def _append(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def run_once(args) -> int:
    t0 = time.time()
    try:
        res = collect(args.top, args.min_value, args.window,
                      not args.include_unprofitable, args.rank_by)
    except Exception as e:  # noqa: BLE001
        print(f"[{_ts()}] FAILED: {e}", flush=True)
        return 1
    day = res["now"].strftime("%Y-%m-%d")
    _append(SNAP_DIR / f"{day}.jsonl", res["snapshot"])
    _append(POS_DIR / f"{day}.jsonl", res["per_whale"])
    f = res["filters"]
    print(f"[{_ts()}] {f['read']}/{f['eligible']} whales read "
          f"({f['failed']} failed), {len(res['snapshot'])} coins, "
          f"{len(res['per_whale'])} positions, {time.time()-t0:.0f}s", flush=True)
    for r in res["snapshot"][:args.show]:
        print(f"    {r['coin']:8} long ${r['long_usd']:>14,.0f}  "
              f"short ${r['short_usd']:>14,.0f}  skew {r['skew']:>+6.2f}  "
              f"({r['n_long']}L/{r['n_short']}S)", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--top", type=int, default=150, help="whales to read per snapshot")
    ap.add_argument("--rank-by", default="roi", choices=["roi", "volume", "pnl", "value"],
                    help="cohort ranking; 'value' selects idle vaults, see select_whales")
    ap.add_argument("--min-value", type=float, default=1_000_000.0)
    ap.add_argument("--window", default="month", choices=["day", "week", "month", "allTime"])
    ap.add_argument("--include-unprofitable", action="store_true",
                    help="drop the profit filter (keeps merely-large accounts)")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    if args.once:
        return run_once(args)
    print(f"whale collector starting: top {args.top} >${args.min_value:,.0f} "
          f"profitable-on-{args.window}, every {args.interval}s", flush=True)
    while True:
        run_once(args)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

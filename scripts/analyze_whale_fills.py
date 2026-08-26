"""Reverse-engineer what profitable Hyperliquid traders actually do.

Every strategy this project tested was invented here and then measured against
the market. This inverts that: Hyperliquid publishes the full fill history of
any address, and the leaderboard identifies who is profitable, so the winning
strategies can be read off directly instead of guessed at.

Per fill the venue gives coin, px, sz, side, time, fee, crossed (taker/maker),
dir ("Open Long" / "Close Short" / ...) and closedPnl. That is enough to
reconstruct round trips and fingerprint a trader: what they trade, how long they
hold, whether they pay or earn the spread, how concentrated they are, and where
the money actually comes from.

Cohort filters exist because the leaderboard is not a list of traders:
  - accountValue floor        -- ignore accounts too small to matter
  - profitable over window    -- obvious
  - 0 < roi < ROI_CEILING     -- a deposit reads as a 438,185x "return"
  - volume floor              -- accounts with $0 volume did not trade at all;
                                 several of the top ROI rows are vaults

Writes data/whale_fills/{addr}.jsonl (raw) and a ranked fingerprint table.

Usage:
  python3 scripts/analyze_whale_fills.py --top 40
  python3 scripts/analyze_whale_fills.py --top 100 --min-volume 50000000
  python3 scripts/analyze_whale_fills.py --report-only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

FILLS_DIR = PROJECT_ROOT / "data" / "whale_fills"
REPORT = PROJECT_ROOT / "data" / "whale_fingerprints.json"
INFO_URL = "https://api.hyperliquid.xyz/info"

ROI_CEILING = 5.0          # 500%/month; above this is a deposit artifact
SLEEP_S = 0.35          # public info API 429s below roughly this


def _post(payload: dict, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                INFO_URL, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3.0 * (i + 1))
    raise RuntimeError(f"{payload.get('type')} failed: {last}")


def fetch_fills(addr: str, days: int = 30, max_pages: int = 8) -> list[dict]:
    """Fill history for one address, paged backwards through userFillsByTime.

    Plain userFills caps at 2000, which for an active trader is a few days --
    measured on one, 2000 fills covered 4.2 days while paging the time endpoint
    returned 4737 over 29.9. Hold times and rates computed off the capped window
    describe the last few days, not the strategy.
    """
    cursor = int((time.time() - days * 86400) * 1000)
    out, seen = [], set()
    for _ in range(max_pages):
        batch = _post({"type": "userFillsByTime", "user": addr, "startTime": cursor})
        fresh = [f for f in batch if f.get("tid") not in seen]
        if not fresh:
            break
        out.extend(fresh)
        seen.update(f.get("tid") for f in fresh)
        nxt = max(f["time"] for f in fresh) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(SLEEP_S)
    out.sort(key=lambda f: f["time"])
    return out


def _w(row: dict, field: str, window: str = "month") -> float:
    for k, v in row.get("windowPerformances", []):
        if k == window:
            try:
                return float(v.get(field, 0))
            except Exception:
                return 0.0
    return 0.0


def select_traders(rows: list[dict], *, top: int, min_value: float,
                   min_volume: float, window: str = "month") -> list[dict]:
    """Accounts that are sized, profitable, and actually traded."""
    out = []
    for r in rows:
        try:
            av = float(r.get("accountValue", 0))
        except Exception:
            continue
        pnl, roi, vlm = _w(r, "pnl", window), _w(r, "roi", window), _w(r, "vlm", window)
        if av < min_value or pnl <= 0 or vlm < min_volume:
            continue
        if not (0 < roi < ROI_CEILING):
            continue
        out.append({"addr": r["ethAddress"], "name": r.get("displayName") or "",
                    "account_value": av, "pnl": pnl, "roi": roi, "volume": vlm})
    out.sort(key=lambda d: d["roi"], reverse=True)
    return out[:top]


def fingerprint(fills: list[dict]) -> dict:
    """Reduce a fill history to how the trader behaves.

    Round trips are reconstructed from `dir`: an Open starts a leg, a Close ends
    it and carries closedPnl. Positions opened before the fill window have no
    Open, so their Close is counted for P&L but not for hold time -- counting it
    as a zero-length hold would bias every distribution toward scalping.
    """
    if not fills:
        return {}
    fills = sorted(fills, key=lambda f: f["time"])

    def span_h_guard(fs):
        # only call it truncated if the window is also short; a paged history
        # can legitimately exceed 2000 fills.
        return (fs[-1]["time"] - fs[0]["time"]) / 3_600_000.0 < 24 * 7
    opens: dict[tuple, list] = defaultdict(list)
    holds, pnls, coins = [], [], Counter()
    taker = maker = 0
    fees = 0.0

    for f in fills:
        coin, d = f.get("coin"), (f.get("dir") or "")
        try:
            ts = int(f["time"]); pnl = float(f.get("closedPnl") or 0)
            fees += float(f.get("fee") or 0)
        except Exception:
            continue
        coins[coin] += 1
        if f.get("crossed"):
            taker += 1
        else:
            maker += 1
        side = "long" if "Long" in d else ("short" if "Short" in d else None)
        if side is None:
            continue
        key = (coin, side)
        if d.startswith("Open"):
            opens[key].append(ts)
        elif d.startswith("Close"):
            pnls.append(pnl)
            if opens[key]:
                holds.append((ts - opens[key].pop(0)) / 3_600_000.0)  # hours

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    n_fills = len(fills)
    span_h = (fills[-1]["time"] - fills[0]["time"]) / 3_600_000.0 or 1.0
    top_coin, top_n = (coins.most_common(1)[0] if coins else ("?", 0))

    return {
        # userFills caps at 2000. For an active trader that can be minutes of
        # history, which makes fills_per_day and hold times meaningless -- the
        # opens for early closes fall outside the window entirely.
        "truncated": n_fills >= 2000 and span_h_guard(fills),
        "n_fills": n_fills,
        "n_closes": len(pnls),
        "span_hours": round(span_h, 1),
        "fills_per_day": round(n_fills / (span_h / 24), 1) if span_h else 0,
        "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "net_closed_pnl": round(sum(pnls), 2),
        "fees_paid": round(fees, 2),
        "median_hold_h": round(statistics.median(holds), 2) if holds else None,
        "p90_hold_h": round(statistics.quantiles(holds, n=10)[-1], 2) if len(holds) > 9 else None,
        "taker_pct": round(taker / (taker + maker), 3) if (taker + maker) else None,
        "n_coins": len(coins),
        "top_coin": top_coin,
        "top_coin_share": round(top_n / n_fills, 3) if n_fills else None,
    }


def classify(fp: dict) -> str:
    """Coarse archetype, so distinct playbooks are visible in the table."""
    if not fp or fp.get("median_hold_h") is None:
        return "unknown"
    h, taker = fp["median_hold_h"], (fp.get("taker_pct") or 0)
    if taker < 0.35 and fp["fills_per_day"] > 50:
        return "market-maker"
    if h < 0.5:
        return "scalper"
    if h < 8:
        return "intraday"
    if h < 72:
        return "swing"
    return "position"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-value", type=float, default=1_000_000.0)
    ap.add_argument("--min-volume", type=float, default=10_000_000.0)
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to page fill history")
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild the table from cached fills, no network")
    args = ap.parse_args()

    FILLS_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    if args.report_only:
        traders = [{"addr": p.stem, "roi": None, "account_value": None}
                   for p in FILLS_DIR.glob("*.jsonl")]
    else:
        from collect_whale_positions import fetch_leaderboard
        rows = fetch_leaderboard()
        traders = select_traders(rows, top=args.top, min_value=args.min_value,
                                 min_volume=args.min_volume)
        print(f"leaderboard {len(rows):,} -> {len(traders)} genuine traders "
              f"(>${args.min_value:,.0f}, profitable, roi<{ROI_CEILING:.0%}, "
              f"vlm>${args.min_volume:,.0f})\n", flush=True)

    for t in traders:
        path = FILLS_DIR / f"{t['addr']}.jsonl"
        if args.report_only or path.exists():
            fills = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        else:
            try:
                fills = fetch_fills(t["addr"], days=args.days)
            except Exception as e:  # noqa: BLE001
                print(f"  {t['addr'][:10]} FAILED {e}", flush=True)
                continue
            path.write_text("".join(json.dumps(f) + "\n" for f in fills))
            time.sleep(SLEEP_S)
        fp = fingerprint(fills)
        if not fp:
            continue
        fp.update({"addr": t["addr"], "roi": t.get("roi"),
                   "account_value": t.get("account_value"),
                   "archetype": classify(fp)})
        results.append(fp)

    # A trader with no losing closes has PF = infinity, not 0. Sorting on
    # `or 0` put the cleanest records at the bottom of the table.
    def _pf_key(r):
        pf = r.get("profit_factor")
        if pf is None:
            return float("inf") if r.get("n_closes") else -1.0
        return pf
    results.sort(key=_pf_key, reverse=True)
    REPORT.write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "traders": results}, indent=2))

    print(f"{'addr':12}{'archetype':13}{'PF':>7}{'WR':>7}{'closes':>8}"
          f"{'med hold h':>12}{'fills/day':>11}{'taker%':>8}{'coins':>7}{'top coin':>10}")
    for r in results:
        pf = r.get("profit_factor")
        pf_s = "inf" if pf is None and r.get("n_closes") else ("n/a" if pf is None else f"{pf:.2f}")
        trunc = "*" if r.get("truncated") else " "
        print(f"{r['addr'][:10]:12}{r['archetype']:13}"
              f"{pf_s:>7}{trunc}{(r['win_rate'] or 0):>6.1%}"
              f"{r['n_closes']:>8}{(r['median_hold_h'] or 0):>12.2f}"
              f"{r['fills_per_day']:>11.0f}{(r['taker_pct'] or 0):>8.0%}"
              f"{r['n_coins']:>7}{str(r['top_coin'])[:9]:>10}")
    print(f"\nwrote {REPORT}  ({len(results)} traders)")

    arch = Counter(r["archetype"] for r in results)
    print("\narchetypes:", dict(arch.most_common()))
    for a in arch:
        sub = [r["profit_factor"] for r in results
               if r["archetype"] == a and r["profit_factor"]]
        if sub:
            print(f"  {a:13} n={len(sub):>3}  median PF {statistics.median(sub):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

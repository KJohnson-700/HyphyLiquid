"""What do the profitable Hyperliquid traders actually DO?

Consumes the paged fill histories from analyze_whale_fills.py and answers the
questions that decide what to build:

  1. which archetypes make money, weighted by how much they trade
  2. how hold time and maker/taker relate to profit factor
  3. which coins the profitable cohort concentrates in
  4. whether entries lean momentum or mean-reversion -- for every Open fill,
     where price had just been over the prior 1h/6h/24h

(4) is the one that names a strategy. A trader who consistently opens longs
after price has fallen is mean-reverting; one who opens longs after it has
risen is trend-following. Our lanes are all reversion, so if the profitable
cohort leans the other way that is the finding.

Compares profitable vs unprofitable traders throughout, because a pattern both
groups share is not an edge -- it is just how everyone trades this venue.

Usage:
  python3 scripts/whale_strategy_patterns.py
  python3 scripts/whale_strategy_patterns.py --min-closes 50
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
FILLS_DIR = PROJECT_ROOT / "data" / "whale_fills"
FINGERPRINTS = PROJECT_ROOT / "data" / "whale_fingerprints.json"
CANDLES = PROJECT_ROOT / "data" / "candle_panel.csv"
OUT = PROJECT_ROOT / "data" / "whale_strategy_patterns.json"


def load_candles() -> dict:
    if not CANDLES.exists():
        return {}
    df = pd.read_csv(CANDLES)
    df["ts"] = pd.to_datetime(df.ts, utc=True, errors="coerce")
    out = {}
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("ts")
        out[sym] = (g.ts.values.astype("datetime64[ms]").astype("int64"),
                    g.close.astype(float).values)
    return out


def prior_move(candles: dict, coin: str, ts_ms: int, hours: int):
    """Percent move over the `hours` before ts_ms. None if uncovered."""
    c = candles.get(coin)
    if not c:
        return None
    times, closes = c
    import numpy as np
    i = int(np.searchsorted(times, ts_ms) - 1)
    j = int(np.searchsorted(times, ts_ms - hours * 3_600_000) - 1)
    if i < 0 or j < 0 or i >= len(closes) or j >= len(closes) or i == j:
        return None
    if closes[j] == 0:
        return None
    return (closes[i] - closes[j]) / closes[j] * 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-closes", type=int, default=30,
                    help="ignore traders with too few round trips to characterise")
    args = ap.parse_args()

    if not FINGERPRINTS.exists():
        print("run analyze_whale_fills.py first", file=sys.stderr)
        return 1
    fps = {f["addr"]: f for f in json.loads(FINGERPRINTS.read_text())["traders"]}
    candles = load_candles()
    print(f"fingerprints: {len(fps)}   candle symbols: {len(candles)}\n")

    def is_winner(f):
        pf = f.get("profit_factor")
        return (pf is None and f.get("n_closes")) or (pf is not None and pf > 1.3)

    usable = {a: f for a, f in fps.items() if (f.get("n_closes") or 0) >= args.min_closes}
    win = {a: f for a, f in usable.items() if is_winner(f)}
    lose = {a: f for a, f in usable.items() if not is_winner(f)}
    print(f"usable (>= {args.min_closes} closes): {len(usable)}   "
          f"winners PF>1.3: {len(win)}   rest: {len(lose)}\n")

    # ---- 1. archetypes -------------------------------------------------
    print("=== archetype: share of winners vs losers ===")
    aw, al = Counter(f["archetype"] for f in win.values()), Counter(f["archetype"] for f in lose.values())
    print(f"{'archetype':14}{'winners':>9}{'losers':>8}{'win share':>11}")
    for a in sorted(set(aw) | set(al)):
        tot = aw[a] + al[a]
        print(f"{a:14}{aw[a]:>9}{al[a]:>8}{(aw[a]/tot if tot else 0):>10.0%}")

    # ---- 2. hold time and taker ----------------------------------------
    def med(d, k):
        v = [f[k] for f in d.values() if f.get(k) is not None]
        return statistics.median(v) if v else None
    print("\n=== winners vs losers, median of each trait ===")
    print(f"{'trait':22}{'winners':>12}{'losers':>12}")
    for k, label in (("median_hold_h", "hold hours"), ("taker_pct", "taker share"),
                     ("fills_per_day", "fills/day"), ("n_coins", "coins traded"),
                     ("top_coin_share", "top-coin conc")):
        w, l = med(win, k), med(lose, k)
        fw = f"{w:.2f}" if w is not None else "n/a"
        fl = f"{l:.2f}" if l is not None else "n/a"
        print(f"{label:22}{fw:>12}{fl:>12}")

    # ---- 3. coin preference --------------------------------------------
    print("\n=== coins the winners concentrate in ===")
    cw, cl = Counter(), Counter()
    for a, f in win.items():
        cw[f["top_coin"]] += 1
    for a, f in lose.items():
        cl[f["top_coin"]] += 1
    print(f"{'coin':14}{'winners':>9}{'losers':>8}")
    for coin, n in cw.most_common(12):
        print(f"{str(coin)[:13]:14}{n:>9}{cl[coin]:>8}")

    # ---- 4. momentum or reversion --------------------------------------
    # Winners alone prove nothing here: in a month where price rose, every
    # cohort's entries follow a rise. The loser column is the control -- only a
    # DIFFERENCE between the two is evidence of an edge.
    print("\n=== entry lean: price move BEFORE each Open, winners vs losers ===")
    buckets = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    scanned = 0
    for group, addrs in (("win", win), ("lose", lose)):
      for addr in addrs:
        fp = FILLS_DIR / f"{addr}.jsonl"
        if not fp.exists():
            continue
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            try:
                f = json.loads(line)
            except Exception:
                continue
            d = f.get("dir") or ""
            if not d.startswith("Open"):
                continue
            side = "long" if "Long" in d else "short"
            for h in (1, 6, 24):
                mv = prior_move(candles, f.get("coin"), int(f["time"]), h)
                if mv is not None:
                    buckets[group][side][h].append(mv)
                    scanned += 1
    if scanned:
        print(f"{'side':7}{'win':>7}{'winners':>12}{'losers':>12}{'diff':>10}   verdict")
        for side in ("long", "short"):
            for h in (1, 6, 24):
                vw, vl = buckets["win"][side][h], buckets["lose"][side][h]
                if len(vw) < 20 or len(vl) < 20:
                    continue
                mw, ml = statistics.median(vw), statistics.median(vl)
                diff = mw - ml
                if abs(diff) < 0.05:
                    verdict = "no difference -- not an edge"
                elif side == "long":
                    verdict = ("winners buy MORE strength" if diff > 0
                               else "winners buy MORE weakness")
                else:
                    verdict = ("winners short MORE strength" if diff > 0
                               else "winners short MORE weakness")
                print(f"{side:7}{h:>6}h{mw:>11.3f}%{ml:>11.3f}%{diff:>+9.3f}%   {verdict}")
        print("\n  (medians of the % price move in the window before each Open;\n"
              "   only the winners-minus-losers difference is evidence)")
    else:
        print("  no overlap between fill times and candle_panel coverage")

    OUT.write_text(json.dumps({
        "usable": len(usable), "winners": len(win), "losers": len(lose),
        "archetype_winners": dict(aw), "archetype_losers": dict(al),
        "winner_top_coins": dict(cw.most_common(20)),
    }, indent=2))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

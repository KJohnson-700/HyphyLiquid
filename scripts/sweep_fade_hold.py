"""Does the funding-negative fade signal work if you stop cutting it off early?

The fade lane holds a 4h median. Measured across 349 profitable Hyperliquid
traders, the 2-8h band is the worst on the whole curve (53% winners, median PF
1.33) while 24-72h is the best (74%, 2.63). Before cutting the lane, test
whether its trigger is bad or only its holding period.

Sweeps max_hold_h against a stop/TP width multiplier, because extending the
hold alone is not a fair test: a stop tuned for a 4h move just gets more time to
be hit, so a longer hold needs a wider band to mean anything.

Costs and the live position cap are applied, so numbers are comparable to the
graduation scorecard rather than to the old inflated ones.

Usage:
  python3 scripts/sweep_fade_hold.py
  python3 scripts/sweep_fade_hold.py --symbols HYPE,SOL
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from strategy_search import load_hl_with_funding, signal_funding_neg_fade  # noqa: E402
from paper_funding_neg_fade import (  # noqa: E402
    NEG_THRESHOLD, PER_ASSET_POLICY, COST_BPS_ROUND_TRIP,
)
from src.strategy.position_cap import apply_position_cap  # noqa: E402

HOLDS = [4, 8, 12, 24, 48, 72]
WIDTHS = [1.0, 1.5, 2.0, 3.0]


def simulate(df: pd.DataFrame, sig, stop_pct: float, tp_pct: float,
             max_hold_h: int, symbol: str) -> list[dict]:
    """Long-only fade: enter next bar after a signal, exit on stop/TP/timeout."""
    closes = df["close"].values
    highs = df["high"].values if "high" in df else closes
    lows = df["low"].values if "low" in df else closes
    times = df.index
    sig_v = sig.to_numpy() if hasattr(sig, "to_numpy") else sig

    trades, i, n = [], 1, len(df)
    while i < n - 1:
        if not (sig_v[i] and not sig_v[i - 1] and closes[i] > 0):
            i += 1
            continue
        entry = float(closes[i])
        sl, tp = entry * (1 - stop_pct), entry * (1 + tp_pct)
        exit_i, exit_px, reason = None, None, None
        for j in range(i + 1, min(i + 1 + max_hold_h, n)):
            if float(lows[j]) <= sl:
                exit_i, exit_px, reason = j, sl, "stop"
                break
            if float(highs[j]) >= tp:
                exit_i, exit_px, reason = j, tp, "tp"
                break
        if exit_i is None:
            exit_i = min(i + max_hold_h, n - 1)
            exit_px, reason = float(closes[exit_i]), "timeout"
        gross = (exit_px - entry) / entry * 100.0
        net = gross - COST_BPS_ROUND_TRIP / 100.0
        trades.append({
            "symbol": symbol, "entry_ts": str(times[i]), "exit_ts": str(times[exit_i]),
            "net_pct": net, "reason": reason,
            "net_pnl_usd": net,  # position cap sorts on ts only; keep units consistent
        })
        i = exit_i + 1          # no overlapping positions in the same symbol
    return trades


def pf(vals) -> float:
    w = sum(v for v in vals if v > 0)
    l = -sum(v for v in vals if v < 0)
    return w / l if l > 0 else float("inf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC,ETH,HYPE,SOL")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    loaded = {}
    for s in symbols:
        try:
            r = load_hl_with_funding(s)
            df = r[0] if isinstance(r, tuple) else r
            loaded[s] = (df, signal_funding_neg_fade(
                df, funding_col="funding_actual", neg_threshold=NEG_THRESHOLD))
        except Exception as e:  # noqa: BLE001
            print(f"  {s}: load failed: {e}")
    if not loaded:
        return 1

    print(f"fade signal, venue-aligned panel, costs {COST_BPS_ROUND_TRIP}bps, "
          f"position cap 3\n")
    print(f"{'hold':>6}{'width':>7}{'n':>6}{'PF':>8}{'win':>7}{'net %':>9}{'timeout%':>10}")
    best = None
    for hold in HOLDS:
        for wmul in WIDTHS:
            all_tr = []
            for s, (df, sig) in loaded.items():
                base = PER_ASSET_POLICY.get(s, {"stop_pct": 0.012, "tp_pct": 0.016})
                all_tr += simulate(df, sig, base["stop_pct"] * wmul,
                                   base["tp_pct"] * wmul, hold, s)
            if not all_tr:
                continue
            capped = [t for t in apply_position_cap(all_tr, 3) if t["admitted"]]
            v = [t["net_pct"] for t in capped]
            if len(v) < 8:
                continue
            p = pf(v)
            wr = sum(1 for x in v if x > 0) / len(v)
            to = sum(1 for t in capped if t["reason"] == "timeout") / len(capped)
            flag = ""
            if best is None or (p != float("inf") and p > best[0]):
                if p != float("inf"):
                    best = (p, hold, wmul, len(v), wr, sum(v))
                    flag = "  <-- best"
            print(f"{hold:>5}h{wmul:>7.1f}{len(v):>6}{p:>8.2f}{wr:>7.0%}"
                  f"{sum(v):>8.2f}%{to:>9.0%}{flag}")
    if best:
        p, hold, wmul, n, wr, tot = best
        print(f"\nbest: hold {hold}h, stop/TP x{wmul}  ->  n={n}  PF={p:.2f}  "
              f"win {wr:.0%}  total {tot:+.2f}%")
        print(f"current live config is HYPE 4h x1.0 -- gate needs PF >= 1.5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

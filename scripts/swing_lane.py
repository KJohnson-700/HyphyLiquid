"""Swing lane — built from what profitable Hyperliquid traders actually do.

Measured across 349 profitable accounts (scripts/analyze_whale_fills.py):
  - the 2-8h holding band is the worst on the venue: 53% winners, median PF 1.33
  - 24-72h is the best: 74% winners, median PF 2.63
  - winners open after price has already moved; losers trade flat tape
    (24h prior move, winners minus losers: longs +0.77%, shorts +0.28%)

So the lane is: wait for a real move, take it in a chosen direction, hold for
days not hours, and use a stop wide enough that noise does not take you out.

Selection rule is deliberately strict. The 25-day version of the fade sweep
produced PF 11.55 at n=16 -- the best of 24 configurations on ~30 trades, which
is noise. Here a configuration is only accepted if it clears the gate in BOTH
independent halves of the sample, so a single lucky window cannot promote it.

Usage:
  python3 scripts/swing_lane.py
  python3 scripts/swing_lane.py --symbols HYPE,ETH,ZEC --min-n 25
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

from src.strategy.position_cap import apply_position_cap  # noqa: E402

PANEL = PROJECT_ROOT / "data" / "candle_panel.csv"
OUT = PROJECT_ROOT / "data" / "swing_lane_calibration.json"
COST_PCT = 0.08                      # 8bps round trip, same as the fade lane
SPLIT = pd.Timestamp("2026-05-01")   # halves for out-of-sample checking

MOVES = [2.0, 4.0, 6.0]              # |24h move| that counts as "has moved", %
HOLDS = [24, 48, 72]
STOPS = [0.04, 0.06, 0.08]           # wide, per the whale hold times
RR = 1.5                             # take-profit = stop * RR


def load_panel(symbols: list[str]) -> dict:
    df = pd.read_csv(PANEL)
    df["ts"] = pd.to_datetime(df.ts, errors="coerce")
    out = {}
    for s in symbols:
        d = df[df.symbol == s].sort_values("ts").reset_index(drop=True)
        if len(d) < 500:
            continue
        d["move24"] = d.close.pct_change(24) * 100.0
        out[s] = d
    return out


def simulate(d: pd.DataFrame, symbol: str, *, move_thr: float, direction: str,
             hold_h: int, stop: float) -> list[dict]:
    """Enter the bar after a qualifying move; exit on stop, target, or timeout.

    `direction` is which way we take the move: "momentum" goes with it,
    "reversion" against it. Both are tested because the whale data showed
    winners opening both longs and shorts after upward moves -- the common
    factor was movement, not side.
    """
    close = d.close.values
    high = d.high.values if "high" in d else close
    low = d.low.values if "low" in d else close
    mv = d.move24.values
    ts = d.ts.values
    tp = stop * RR

    trades, i, n = 25, 0, len(d)
    i = 25
    while i < n - 1:
        m = mv[i]
        if pd.isna(m) or abs(m) < move_thr:
            i += 1
            continue
        up = m > 0
        long_ = up if direction == "momentum" else (not up)
        entry = float(close[i])
        if entry <= 0:
            i += 1
            continue
        sl = entry * (1 - stop) if long_ else entry * (1 + stop)
        tgt = entry * (1 + tp) if long_ else entry * (1 - tp)
        ex_i = ex_px = reason = None
        for j in range(i + 1, min(i + 1 + hold_h, n)):
            if long_:
                if float(low[j]) <= sl:
                    ex_i, ex_px, reason = j, sl, "stop"; break
                if float(high[j]) >= tgt:
                    ex_i, ex_px, reason = j, tgt, "tp"; break
            else:
                if float(high[j]) >= sl:
                    ex_i, ex_px, reason = j, sl, "stop"; break
                if float(low[j]) <= tgt:
                    ex_i, ex_px, reason = j, tgt, "tp"; break
        if ex_i is None:
            ex_i = min(i + hold_h, n - 1)
            ex_px, reason = float(close[ex_i]), "timeout"
        gross = ((ex_px - entry) / entry * 100.0) * (1 if long_ else -1)
        if not isinstance(trades, list):
            trades = []
        trades.append({
            "symbol": symbol, "entry_ts": str(ts[i]), "exit_ts": str(ts[ex_i]),
            "net_pct": gross - COST_PCT, "reason": reason,
            "side": "long" if long_ else "short",
        })
        i = ex_i + 1                 # never overlap within a symbol
    return trades if isinstance(trades, list) else []


def pf(v) -> float:
    w = sum(x for x in v if x > 0); l = -sum(x for x in v if x < 0)
    return w / l if l > 0 else float("inf")


def halves(trades):
    def t(x):
        v = pd.Timestamp(x["entry_ts"])
        return v.tz_localize(None) if v.tzinfo else v
    return ([x for x in trades if t(x) < SPLIT], [x for x in trades if t(x) >= SPLIT])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="HYPE,ETH,BTC,SOL,ZEC,PUMP,UNI,LIT,"
                                         "xyz:SKHX,xyz:SNDK,xyz:MU,xyz:GOLD")
    ap.add_argument("--min-n", type=int, default=25, help="min trades per half")
    ap.add_argument("--min-pf", type=float, default=1.5)
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    panels = load_panel(symbols)
    print(f"panel: {len(panels)} symbols with >=500 bars\n")

    survivors = []
    for sym, d in panels.items():
        best = None
        for direction in ("momentum", "reversion"):
            for mv in MOVES:
                for hold in HOLDS:
                    for stop in STOPS:
                        tr = simulate(d, sym, move_thr=mv, direction=direction,
                                      hold_h=hold, stop=stop)
                        h1, h2 = halves(tr)
                        if len(h1) < args.min_n or len(h2) < args.min_n:
                            continue
                        p1, p2 = pf([x["net_pct"] for x in h1]), pf([x["net_pct"] for x in h2])
                        # BOTH halves must clear the gate -- a config that only
                        # works in one half is a window, not an edge.
                        if p1 < args.min_pf or p2 < args.min_pf:
                            continue
                        allv = [x["net_pct"] for x in tr]
                        cand = {"symbol": sym, "direction": direction, "move_thr": mv,
                                "hold_h": hold, "stop": stop, "tp": round(stop * RR, 4),
                                "n": len(tr), "pf": round(pf(allv), 2),
                                "pf_h1": round(p1, 2), "pf_h2": round(p2, 2),
                                "win": round(sum(1 for x in allv if x > 0) / len(allv), 3),
                                "net_pct": round(sum(allv), 2)}
                        if best is None or cand["pf"] > best["pf"]:
                            best = cand
        if best:
            survivors.append(best)
            print(f"  {sym:11} {best['direction']:10} move>{best['move_thr']}%  "
                  f"hold {best['hold_h']}h  stop {best['stop']:.0%}  "
                  f"n={best['n']:>4}  PF {best['pf']:>5.2f}  "
                  f"(H1 {best['pf_h1']}, H2 {best['pf_h2']})  net {best['net_pct']:+.1f}%")
        else:
            print(f"  {sym:11} no configuration clears PF>={args.min_pf} in BOTH halves")

    OUT.write_text(json.dumps({"cost_pct": COST_PCT, "rr": RR,
                               "survivors": survivors}, indent=2))
    print(f"\n{len(survivors)}/{len(panels)} symbols produced a config that holds "
          f"in both halves\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

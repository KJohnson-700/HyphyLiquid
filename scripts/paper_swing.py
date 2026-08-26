"""Forward paper record for the swing lane.

swing_lane.py calibrates and validates; this runs the surviving configs and
writes closed trades in the shape the graduation scorecard reads, so the lane
can actually accumulate the forward evidence Gate 2 requires. A backtest result
with no forward record can never be promoted.

It calls swing_lane.simulate directly rather than reimplementing the loop. Two
implementations of the same strategy already diverged once in this project --
the fade sweep said ETH PF 1.65 while the paper sim said 1.04, because one was
edge-triggered and the other level-triggered. One implementation, one answer.

Trade ids are deterministic on (symbol, entry bar), so re-running never
duplicates: paper ids once carried random hex and every rerun re-appended the
same trades, inflating n, PF and win rate -- the exact numbers the gates score.

Usage:
  python3 scripts/paper_swing.py
  python3 scripts/paper_swing.py --rebuild     # discard and regenerate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from swing_lane import load_panel, simulate  # noqa: E402
from src.strategy.position_cap import apply_position_cap  # noqa: E402

CALIB = PROJECT_ROOT / "data" / "swing_lane_calibration.json"
OUT = PROJECT_ROOT / "data" / "paper_swing_positions.jsonl"
BANKROLL = 1_000.0
RISK_USD = 10.0            # 1% of bankroll, same framework as the fade lane
MAX_OPEN = 3


def trade_id(symbol: str, entry_ts: str) -> str:
    """Stable identity: symbol + entry bar. No random component, ever."""
    return f"swing-{symbol}-{str(entry_ts)[:16].replace(' ', 'T')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if not CALIB.exists():
        print("run scripts/swing_lane.py first", file=sys.stderr)
        return 1
    survivors = json.loads(CALIB.read_text())["survivors"]
    if not survivors:
        print("no calibrated swing configs; nothing to run")
        return 0

    panels = load_panel([s["symbol"] for s in survivors])
    existing = {}
    if OUT.exists() and not args.rebuild:
        for line in OUT.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    existing[r["paper_id"]] = r
                except Exception:
                    continue

    fresh = []
    for cfg in survivors:
        sym = cfg["symbol"]
        d = panels.get(sym)
        if d is None:
            print(f"  {sym}: no panel coverage, skipping")
            continue
        # size so a stop costs RISK_USD, never max leverage
        notional = RISK_USD / cfg["stop"]
        for t in simulate(d, sym, move_thr=cfg["move_thr"],
                          direction=cfg["direction"], hold_h=cfg["hold_h"],
                          stop=cfg["stop"]):
            pid = trade_id(sym, t["entry_ts"])
            if pid in existing:
                continue
            fresh.append({
                "paper_id": pid, "symbol": sym, "lane": "swing",
                "side": t["side"], "status": "closed",
                "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
                "net_return_pct": round(t["net_pct"], 6),
                "net_pnl_usd": round(t["net_pct"] / 100.0 * notional, 4),
                "notional_usd": round(notional, 2),
                "risk_usd": RISK_USD, "exit_reason": t["reason"],
                "direction": cfg["direction"], "move_thr": cfg["move_thr"],
                "hold_h": cfg["hold_h"], "stop_pct": cfg["stop"],
            })

    all_rows = list(existing.values()) + fresh
    # The simulator runs each symbol independently; live would refuse a fourth
    # concurrent position, so score only what the cap would have admitted.
    capped = apply_position_cap(all_rows, MAX_OPEN)
    admitted = [r for r in capped if r.get("admitted", True)]
    blocked = len(capped) - len(admitted)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r) + "\n" for r in admitted))

    pnl = sum(r["net_pnl_usd"] for r in admitted)
    wins = [r for r in admitted if r["net_pnl_usd"] > 0]
    gw = sum(r["net_pnl_usd"] for r in wins)
    gl = -sum(r["net_pnl_usd"] for r in admitted if r["net_pnl_usd"] < 0)
    pf = gw / gl if gl > 0 else float("inf")
    print(f"swing paper: {len(admitted)} closed (+{len(fresh)} new, "
          f"{blocked} blocked by position cap)  PF={pf:.2f}  "
          f"win={len(wins)/len(admitted):.0%}  cum PnL=${pnl:+.2f}")
    for cfg in survivors:
        n = sum(1 for r in admitted if r["symbol"] == cfg["symbol"])
        print(f"    {cfg['symbol']:10} {cfg['direction']:10} move>{cfg['move_thr']}%  "
              f"hold {cfg['hold_h']}h  stop {cfg['stop']:.0%}  n={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

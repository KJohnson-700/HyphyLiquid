"""Check that the panels the strategy reads actually cover enough history.

The failure this exists to catch: every daemon step reports "ok" while the
funding panel quietly covers only the last day, because the jsonl builders
read files that disk maintenance already deleted. The strategy then finds no
signals and the closed-trade count sits still, which looks identical to
"the market gave us nothing".

A funding panel much shorter than the candle panel is the tell -- the two are
inner-joined before the signal runs, so the join collapses to the shorter one.

It also checks the funding panel is aligned to the venue. asset_ctx.funding is
the rate for the *upcoming* settlement, so any builder deriving funding from
polled snapshots stamps it an hour early and the strategy trades on a rate the
venue has not published -- look-ahead that inflates every result. Correlating
against fundingHistory at 0h vs +1h catches that immediately: a panel that
matches better at +1h is shifted.

Usage:
  python3 scripts/panel_health.py
  python3 scripts/panel_health.py --min-hours 100
Exit code 1 if any traded symbol fails, so it can gate a daemon or a cron.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANDLE = PROJECT_ROOT / "data" / "candle_panel.csv"
FUNDING = PROJECT_ROOT / "data" / "funding_panel.csv"

# the symbols with a calibrated PER_ASSET_POLICY -- the only ones that trade
TRADED = ["BTC", "ETH", "HYPE", "SOL"]


API = "https://api.hyperliquid.xyz/info"


def _venue_funding(symbol: str, start_ms: int) -> pd.DataFrame:
    req = urllib.request.Request(
        API, data=json.dumps({"type": "fundingHistory", "coin": symbol,
                              "startTime": start_ms}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.load(r)
    if not rows:
        return pd.DataFrame(columns=["ts", "venue"])
    df = pd.DataFrame(rows)
    return pd.DataFrame({
        "ts": pd.to_datetime(df.time, unit="ms", utc=True).dt.floor("h"),
        "venue": df.fundingRate.astype(float),
    })


def check_alignment(min_corr: float = 0.97) -> list[str]:
    """Compare the funding panel against the venue at 0h and +1h.

    Returns the symbols that fail. A network problem is reported as a skip, not
    a pass -- an unknown must never read as healthy.
    """
    panel = pd.read_csv(FUNDING)
    panel["ts"] = pd.to_datetime(panel.ts, utc=True)
    start_ms = int((panel.ts.max() - pd.Timedelta(days=5)).timestamp() * 1000)
    bad = []
    print(f"\n{'symbol':8} {'corr@0h':>8} {'corr@+1h':>9}  verdict")
    for sym in TRADED:
        try:
            v = _venue_funding(sym, start_ms)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:8} {'--':>8} {'--':>9}  SKIP (venue unreachable: {e})")
            bad.append(sym)
            continue
        p = panel[panel.symbol == sym][["ts", "funding_actual"]]
        c0 = v.merge(p, on="ts").dropna()
        p1 = p.copy(); p1["ts"] = p1.ts + pd.Timedelta(hours=1)
        c1 = v.merge(p1, on="ts").dropna()
        r0 = c0.venue.corr(c0.funding_actual) if len(c0) > 2 else float("nan")
        r1 = c1.venue.corr(c1.funding_actual) if len(c1) > 2 else float("nan")
        if pd.isna(r0):
            verdict, ok = "SKIP (no overlap)", False
        elif not pd.isna(r1) and r1 > r0:
            verdict, ok = "SHIFTED -- panel is an hour early", False
        elif r0 < min_corr:
            verdict, ok = f"WEAK (< {min_corr})", False
        else:
            verdict, ok = "ok", True
        if not ok:
            bad.append(sym)
        print(f"{sym:8} {r0:>8.4f} {r1:>9.4f}  {verdict}")
    return bad


def _cov(path: Path) -> dict:
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return {
        s: (len(d), d["ts"].min(), d["ts"].max())
        for s, d in df.groupby("symbol")
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hours", type=int, default=100,
                    help="hourly rows a traded symbol needs in BOTH panels")
    ap.add_argument("--max-gap", type=int, default=24,
                    help="hours the funding panel may lag the candle panel by")
    ap.add_argument("--skip-alignment", action="store_true",
                    help="skip the venue alignment check (offline use only)")
    args = ap.parse_args()

    candle, funding = _cov(CANDLE), _cov(FUNDING)
    if not candle or not funding:
        print("FAIL: a panel is missing entirely", file=sys.stderr)
        return 1

    bad = []
    print(f"{'symbol':8} {'candle':>7} {'funding':>8}  {'joined range':>24}")
    for sym in TRADED:
        c, f = candle.get(sym), funding.get(sym)
        if not c or not f:
            print(f"{sym:8} {'--':>7} {'--':>8}  MISSING FROM A PANEL")
            bad.append(sym)
            continue
        # the strategy sees the intersection, not either panel alone
        lo, hi = max(c[1], f[1]), min(c[2], f[2])
        joined = int((hi - lo).total_seconds() // 3600) + 1 if hi >= lo else 0
        flag = ""
        if joined < args.min_hours:
            flag = f"  <-- joined {joined}h < {args.min_hours}h"
            bad.append(sym)
        elif (c[2] - f[2]).total_seconds() / 3600 > args.max_gap:
            flag = "  <-- funding panel is stale vs candles"
            bad.append(sym)
        print(f"{sym:8} {c[0]:>7} {f[0]:>8}  {joined:>5}h {lo:%m-%d %H} -> {hi:%m-%d %H}{flag}")

    if not args.skip_alignment:
        bad = sorted(set(bad) | set(check_alignment()))

    if bad:
        print(f"\nFAIL: {', '.join(bad)}", file=sys.stderr)
        print("Rebuild from the venue:\n"
              "  python3 scripts/build_funding_from_venue.py\n"
              "  python3 scripts/build_candles_from_venue.py", file=sys.stderr)
        return 1
    print("\nok: coverage sufficient and funding aligned to the venue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Keep the funding-negative fade lane accumulating forward-paper trades.

The five bootstrap daemons all collect data; none runs the strategy that is
actually up for promotion. Without this loop the fade lane is frozen at
whatever trades already exist, so a lane can never reach Gate 2 (n>=30 with
>=15 forward trades, and >=2 market regimes).

Each tick, in order:
  1. backfill both panels from DuckDB, then rebuild from freshly captured WS
     data. The jsonl builders only see files still on disk, and disk
     maintenance deletes old ones -- without the DuckDB backfill the funding
     panel silently collapses to the last day or two and the strategy stops
     finding signals while every step still reports "ok".
  2. run paper_funding_neg_fade.py --mode paper (simulation only, no orders)
  3. re-label regimes on closed trades

The tick reports the change in closed-trade count. A daemon that runs clean
but never trades is the failure mode this loop exists to prevent, so a flat
count is called out explicitly rather than left to look like success.

Paper mode never places an order. Live trading in that script is gated
separately on an in-file flag plus a passed testnet bracket proof with a
visible protective stop; this daemon never passes --mode live_trading.

Usage:
  python3 scripts/fade_paper_daemon.py                # hourly
  python3 scripts/fade_paper_daemon.py --once         # single pass
  python3 scripts/fade_paper_daemon.py --interval 900
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PY = str(Path(sys.executable))

# Panels come from the venue, not from local jsonl or DuckDB.
#
# asset_ctx.funding is the rate for the *upcoming* settlement, so every builder
# that derives funding from polled snapshots stamps it an hour early -- the
# simulator then trades on a rate the venue has not published, which is
# look-ahead. build_funding_panel.py and build_panels_from_duckdb.py both do
# this, and while they were in this loop they silently re-corrupted the panel
# every hour (venue alignment fell from 1.0000 to 0.8538 overnight). They are
# deliberately NOT in STEPS. fundingHistory/candleSnapshot are authoritative,
# re-fetchable, and cover more history than the box ever captured locally.
STEPS = [
    ("venue funding", [PY, "scripts/build_funding_from_venue.py"]),
    ("venue candles", [PY, "scripts/build_candles_from_venue.py"]),
    ("panel health",  [PY, "scripts/panel_health.py"]),
    ("fade paper",    [PY, "scripts/paper_funding_neg_fade.py", "--mode", "paper"]),
    ("swing paper",   [PY, "scripts/paper_swing.py"]),
    # Real orders on Hyperliquid TESTNET, armed 2026-08-26 by kslim. This is
    # the evidence Gate 2's decision_path_has_tests actually asks for: a live
    # decision path cannot be proven with a simulator, and testnet had been
    # used exactly once before this. No mainnet funds are involved -- the mode
    # refuses to start unless HYPERLIQUID_ENV=testnet and re-checks the
    # resolved URL. It also refuses signals older than one bar, so a quiet
    # market produces nothing rather than stale entries.
    ("testnet exec",  [PY, "scripts/paper_funding_neg_fade.py",
                       "--mode", "testnet_trading", "--arm-testnet"]),
    # Swing lane on testnet too, armed 2026-08-26. ZEC's PF 1.70 was entirely
    # simulated until now, and simulation has already been wrong in a way only
    # a real venue could reveal: the first live fade signal was rejected
    # because rounding pushed risk 1.2 cents over the cap. Shares the fade
    # lane's open-positions file, so the 3-position cap is portfolio-wide.
    ("swing testnet", [PY, "scripts/swing_testnet.py", "--arm-testnet"]),
    ("regime labels", [PY, "scripts/label_trade_regimes.py"]),
]


FUNDING_PANEL = PROJECT_ROOT / "data" / "funding_panel.csv"
TRADED = ("BTC", "ETH", "HYPE", "SOL")


def _qualifying_bars(hours: int = 24) -> int:
    """Bars in the recent window where funding is negative enough to signal.

    A flat trade count means two very different things. If no bar cleared the
    threshold there was simply nothing to trade and the lane is idle -- correct
    behaviour, not a fault. If bars did clear it and still nothing opened, the
    pipeline is broken. Escalating on the first case trains everyone to ignore
    the warning, so the two are reported differently.

    Returns -1 when the answer is unknown, which is never treated as idle.
    """
    try:
        import pandas as pd
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from paper_funding_neg_fade import NEG_THRESHOLD

        df = pd.read_csv(FUNDING_PANEL)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        recent = df[(df["ts"] >= df["ts"].max() - pd.Timedelta(hours=hours))
                    & (df["symbol"].isin(TRADED))]
        return int((recent["funding_actual"] < NEG_THRESHOLD).sum())
    except Exception:
        return -1


POSITIONS = PROJECT_ROOT / "data" / "paper_funding_neg_fade_positions.jsonl"
STALL_TICKS = 6  # ~6h at the default interval before the flat count is a warning


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _closed_count() -> int:
    """Closed trades on disk. -1 if the file is unreadable, so a read error
    is never mistaken for 'no new trades'."""
    if not POSITIONS.exists():
        return 0
    try:
        import json
        n = 0
        with POSITIONS.open() as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("status") == "closed":
                        n += 1
                except Exception:
                    continue
        return n
    except Exception:
        return -1


# Steps that must succeed before the strategy is allowed to run. If the panel
# is wrong, running the sim on it manufactures numbers rather than surfacing the
# fault -- which is exactly how a broken lane looked healthy for 11 hours.
BLOCKING_STEPS = {"venue funding", "venue candles", "panel health"}


def run_once() -> tuple[int, bool]:
    """Returns (failure count, aborted). Aborted means a required step failed
    and the strategy never ran, which must not be reported as a quiet market."""
    failures = 0
    aborted = False
    for name, cmd in STEPS:
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                               text=True, timeout=1800)
            if r.returncode == 0:
                tail = [l for l in r.stdout.strip().splitlines() if l.strip()]
                print(f"  [{_ts()}] {name}: ok ({time.time()-t0:.0f}s) "
                      f"{tail[-1][:90] if tail else ''}", flush=True)
            else:
                failures += 1
                err = (r.stderr.strip().splitlines() or ["?"])[-1]
                print(f"  [{_ts()}] {name}: FAILED rc={r.returncode} {err[:140]}", flush=True)
                if name in BLOCKING_STEPS:
                    print(f"  [{_ts()}] ABORTING TICK: {name} is required; refusing "
                          f"to run the strategy on a panel that failed its check",
                          flush=True)
                    return failures, True
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"  [{_ts()}] {name}: TIMEOUT", flush=True)
            if name in BLOCKING_STEPS:
                print(f"  [{_ts()}] ABORTING TICK: {name} timed out", flush=True)
                return failures, True
    return failures, aborted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()

    print(f"fade paper daemon starting (interval {args.interval}s, paper mode only)", flush=True)
    flat_ticks = 0
    while True:
        before = _closed_count()
        print(f"[{_ts()}] tick  closed={before}", flush=True)
        failures, aborted = run_once()
        after = _closed_count()

        if aborted:
            # Never let an aborted tick read as a quiet market -- that is the
            # exact confusion the stall detector exists to prevent.
            flat_ticks += 1
            print(f"  [{_ts()}] progress: TICK ABORTED, strategy did not run "
                  f"({flat_ticks} tick(s) without progress)", flush=True)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue

        delta = after - before if before >= 0 and after >= 0 else 0
        if delta > 0:
            flat_ticks = 0
            print(f"  [{_ts()}] progress: +{delta} closed trades (now {after})", flush=True)
        else:
            qual = _qualifying_bars()
            if qual == 0:
                # Nothing crossed the entry threshold; there was no trade to make.
                flat_ticks = 0
                print(f"  [{_ts()}] progress: idle -- no bar under NEG_THRESHOLD "
                      f"in the last 24h across {'/'.join(TRADED)}", flush=True)
            else:
                flat_ticks += 1
                seen = f"{qual} qualifying bar(s)" if qual > 0 else "signal count unknown"
                msg = (f"  [{_ts()}] progress: no new closed trades ({flat_ticks} "
                       f"tick(s) flat, {seen} in last 24h)")
                if flat_ticks >= STALL_TICKS:
                    msg += (f"  <-- STALLED {flat_ticks} ticks with signals present. "
                            f"Check panel coverage: python3 scripts/panel_health.py")
                print(msg, flush=True)
        if failures:
            print(f"  [{_ts()}] {failures} step(s) failed this tick", flush=True)

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

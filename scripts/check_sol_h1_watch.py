"""SOL H1 research watch — strict decision rule.

Per Slim's 2026-08-05 spec: track SOL H1 (BTC/ETH calm) over consecutive
rebuild cycles, with a STRICT decision rule that gates any scope
discussion on:

  1. Repeated confirmation: both 30m and 60m H1 verdicts pass the
     standard promotion gate in N_CONSECUTIVE_CYCLES cycles in a row
     (default: 2).
  2. Fixed calm definition: BTC 30m-realized-vol <= 5 bps/min stdev AND
     ETH <= 8 bps/min stdev. Same constants every cycle so verdicts
     are comparable.
  3. Paper simulation: a paper-only decision loop has produced at
     least PAPER_SIM_MIN_DECISIONS paper decisions tagged with the SOL
     H1 setup. Until that count is non-zero, the watch status is
     "watch-pending-paper".

BTC and HYPE remain under observation; the report includes a brief
status line for each (pulled from existing cycle outputs).

HARD SCOPE: research only. Does NOT touch execution, order_manager,
risk.py, or any live/paper routing. Reads and appends to
`data/sol_h1_watch_log.jsonl` (gitignored, append-only).

Run from cycle:
    python scripts/check_sol_h1_watch.py

Run standalone:
    python scripts/check_sol_h1_watch.py --cycles-required 2 \
        --paper-sim-min-decisions 5
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_relative_value_dislocation import (  # noqa: E402
    ALT_SYMBOLS,
    CONFIRM_MIN_RETURN_PCT,
    FIXED_CALM_VOL_BTC_30M,
    FIXED_CALM_VOL_ETH_30M,
    PROMOTION_N,
    PROMOTION_PF,
    PROMOTION_TOP_WIN_SHARE,
    REF_SYMBOLS,
    PromotionVerdict,
    _attach_isolation_flag,
    _attach_regime_flags,
    _load_asset_ctx_series,
    _load_candles,
    _load_cascades,
    apply_promotion_gate,
    compute_per_event_records,
)

# ----------------------------- constants ---------------------------------- #

WATCH_LOG_PATH = REPO_ROOT / "data" / "sol_h1_watch_log.jsonl"
PAPER_DECISIONS_DIR = REPO_ROOT / "data"
BTC_TRAILING_SWEEP_PATH = REPO_ROOT / "data" / "trailing_sweep_btc_eth_btc_side_b.json"
BTC_B_LANE_TRADES_PATH = REPO_ROOT / "data" / "lane_backtest_btc_eth_fade_or_follow_btc_side_b_trades.jsonl"
LANE_HYPE_B_PATH = REPO_ROOT / "data" / "lane_backtest_alt_range_liq_scalp_hype_side_b_trades.jsonl"

# Strict decision rule (per Slim 2026-08-05).
N_CONSECUTIVE_CYCLES: int = 2
# Minimum paper decisions tagged with the SOL H1 setup before the watch
# can flip from "watch-pending-paper" to "watch-confirmed". Until the
# paper lane is wired, the report just says "not wired".
PAPER_SIM_MIN_DECISIONS: int = 5
# Watched horizons for SOL H1.
WATCH_HORIZONS: tuple[int, ...] = (30, 60)


# ----------------------------- dataclasses ------------------------------- #


@dataclass
class WatchCycleRecord:
    cycle_ts_utc: str
    cascade_count: int
    cascade_count_delta: int
    sol_total_events: int
    sol_h1_30m: dict[str, Any]
    sol_h1_60m: dict[str, Any]
    btc_observation: dict[str, Any]
    hype_observation: dict[str, Any]
    paper_sim: dict[str, Any]
    consecutive_passes: int
    cumulative_passes: int
    status: str  # "watch-pending", "watch-pending-paper", "watch-confirmed", "watch-broken"
    decision_rule: dict[str, Any] = field(default_factory=dict)


# ------------------------- paper sim status ------------------------------ #


def _paper_sim_status() -> dict[str, Any]:
    """Inspect the paper lane for SOL H1-tagged decisions.

    The current paper_decision_loop.py does not tag by playbook, so we
    can only report a count of total paper decisions today. Until
    paper lane is H1-aware, this returns {"wired": False, ...}.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    paper_path = PAPER_DECISIONS_DIR / f"paper_decisions_{today}.jsonl"
    if not paper_path.exists():
        return {
            "wired": False,
            "reason": "paper_decision_loop.py does not yet tag by SOL H1 setup",
            "decisions_today": 0,
            "h1_decisions_today": 0,
            "threshold": PAPER_SIM_MIN_DECISIONS,
            "status": "not_wired",
        }
    n_total = 0
    n_h1 = 0
    try:
        for line in paper_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_total += 1
            # If/when paper loop tags with "playbook": "sol_h1", count it.
            if isinstance(rec, dict) and rec.get("playbook") == "sol_h1":
                n_h1 += 1
    except OSError:
        pass
    if n_h1 >= PAPER_SIM_MIN_DECISIONS:
        sim_status = "ready"
    elif n_h1 > 0:
        sim_status = "partial"
    else:
        sim_status = "not_wired"
    return {
        "wired": n_h1 > 0,
        "decisions_today": n_total,
        "h1_decisions_today": n_h1,
        "threshold": PAPER_SIM_MIN_DECISIONS,
        "status": sim_status,
    }


# -------------------------- BTC / HYPE observation ----------------------- #


def _btc_observation() -> dict[str, Any]:
    """BTC B-side failed_reclaim_continuation + trailing sweep status."""
    out: dict[str, Any] = {
        "btc_b_total_n": None,
        "btc_b_pf": None,
        "btc_b_trailing_n": None,
        "btc_b_trailing_best_pf": None,
    }
    if BTC_TRAILING_SWEEP_PATH.exists():
        try:
            rec = json.loads(BTC_TRAILING_SWEEP_PATH.read_text(encoding="utf-8"))
            # File may be a list of rows, or {"rows": [...]}
            if isinstance(rec, dict):
                rows = rec.get("rows", [])
            else:
                rows = rec if isinstance(rec, list) else []
            if rows:
                out["btc_b_trailing_n"] = max((r.get("n", 0) or 0) for r in rows)
                out["btc_b_trailing_best_pf"] = max(
                    (r.get("profit_factor", 0) or 0) for r in rows
                )
        except (json.JSONDecodeError, OSError):
            pass
    if BTC_B_LANE_TRADES_PATH.exists():
        try:
            rows = [
                json.loads(line)
                for line in BTC_B_LANE_TRADES_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            pf = _pf_from_trades(rows) if rows else None
            out["btc_b_total_n"] = len(rows)
            out["btc_b_pf"] = pf
        except (json.JSONDecodeError, OSError):
            pass
    return out


def _hype_observation() -> dict[str, Any]:
    out: dict[str, Any] = {"hype_b_n": None, "hype_b_pf": None}
    if LANE_HYPE_B_PATH.exists():
        try:
            rows = [
                json.loads(line)
                for line in LANE_HYPE_B_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            if rows:
                out["hype_b_n"] = len(rows)
                out["hype_b_pf"] = _pf_from_trades(rows)
        except (json.JSONDecodeError, OSError):
            pass
    return out


def _pf_from_trades(rows: list[dict]) -> float | None:
    pnls: list[float] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pnl = (
            r.get("pnl")
            or r.get("net_pnl")
            or r.get("net_return_pct")
            or r.get("return_pct")
        )
        if pnl is None:
            continue
        try:
            pnls.append(float(pnl))
        except (TypeError, ValueError):
            continue
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    gross_p = sum(wins)
    gross_l = sum(losses)
    if gross_l == 0:
        return float("inf") if gross_p > 0 else 0.0
    return gross_p / gross_l


# -------------------------- prior cycle state ---------------------------- #


def _load_prior_watch_log() -> list[dict]:
    if not WATCH_LOG_PATH.exists():
        return []
    out: list[dict] = []
    for line in WATCH_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _verdict_to_dict(v: PromotionVerdict | None) -> dict[str, Any]:
    if v is None:
        return {"n": 0, "passed": False, "reason": "no events"}
    d = asdict(v)
    d.pop("extra", None)
    return d


# ----------------------------- main -------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SOL H1 research watch — strict decision rule (research only)"
    )
    parser.add_argument(
        "--cycles-required",
        type=int,
        default=N_CONSECUTIVE_CYCLES,
        help=f"Consecutive cycles required to flip to watch-confirmed (default {N_CONSECUTIVE_CYCLES})",
    )
    parser.add_argument(
        "--paper-sim-min-decisions",
        type=int,
        default=PAPER_SIM_MIN_DECISIONS,
        help=f"Min paper decisions tagged SOL H1 to satisfy paper sim (default {PAPER_SIM_MIN_DECISIONS})",
    )
    args = parser.parse_args()

    print("=" * 78)
    print("SOL H1 RESEARCH WATCH — strict decision rule (research only)")
    print("=" * 78)
    print(f"  Cycles required:    {args.cycles_required}")
    print(f"  Calm BTC:           {FIXED_CALM_VOL_BTC_30M} ({FIXED_CALM_VOL_BTC_30M*1e4:.1f} bps/min stdev)")
    print(f"  Calm ETH:           {FIXED_CALM_VOL_ETH_30M} ({FIXED_CALM_VOL_ETH_30M*1e4:.1f} bps/min stdev)")
    print(f"  Watched horizons:   {WATCH_HORIZONS}")
    print(f"  Paper sim min:      {args.paper_sim_min_decisions}")
    print(f"  Promotion gate:     n>={PROMOTION_N} PF>{PROMOTION_PF} med>0 top_win_share<={PROMOTION_TOP_WIN_SHARE:.0%}")
    print()

    all_cascades = _load_cascades()
    cascade_count = sum(1 for c in all_cascades if c.get("symbol") in ALT_SYMBOLS)
    print(f"  Loaded {len(all_cascades)} total cascades, {cascade_count} in alt universe")

    # Load candles + asset_ctx for SOL/BTC/ETH
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in ("SOL",) + REF_SYMBOLS:
        candles_by_symbol[sym] = _load_candles(sym)
    asset_ctx_by_symbol: dict[str, list[dict]] = {
        "SOL": _load_asset_ctx_series("SOL"),
    }

    # Compute per-event records for SOL cascades only
    sol_cascades = [c for c in all_cascades if c.get("symbol") == "SOL"]
    events: list[dict] = []
    for c in sol_cascades:
        rec = compute_per_event_records(
            c, candles_by_symbol, asset_ctx_by_symbol, all_cascades, WATCH_HORIZONS
        )
        if rec is not None:
            events.append(rec)
    print(f"  SOL events evaluated: {len(events)}")

    # Apply FIXED calm thresholds
    events = _attach_regime_flags(events, FIXED_CALM_VOL_BTC_30M, FIXED_CALM_VOL_ETH_30M)
    events = _attach_isolation_flag(events, all_cascades)

    # Filter to H1 (BTC/ETH calm) and apply promotion gate at each horizon
    h1_buckets: dict[int, list[dict]] = {}
    h1_verdicts: dict[int, PromotionVerdict] = {}
    for h in WATCH_HORIZONS:
        bucket = [e for e in events if e["btc_calm"] and e["eth_calm"]]
        h1_buckets[h] = bucket
        v = apply_promotion_gate(bucket, h)
        v.symbol = "SOL"
        v.playbook = "H1_btc_eth_calm"
        h1_verdicts[h] = v
    print()

    # Print per-horizon verdict
    for h in WATCH_HORIZONS:
        v = h1_verdicts[h]
        pf = 999.0 if v.pf == float("inf") else v.pf
        verdict = "PASS" if v.passed else v.reason
        print(
            f"  SOL H1 {h}m: n={v.n:>3} WR={v.win_rate*100:>5.1f}% "
            f"avg={v.avg_pnl_pct:>+8.4f}% med={v.median_pnl_pct:>+8.4f}% "
            f"PF={pf:>5.2f} top={v.top_win_share*100:>5.1f}%  {verdict}"
        )

    # Load prior watch log
    prior = _load_prior_watch_log()
    cascade_count_delta = cascade_count - (
        prior[-1].get("cascade_count", cascade_count) if prior else cascade_count
    )
    prior_consec = prior[-1].get("consecutive_passes", 0) if prior else 0
    prior_cum = prior[-1].get("cumulative_passes", 0) if prior else 0

    # Update consecutive + cumulative
    both_pass = all(h1_verdicts[h].passed for h in WATCH_HORIZONS)
    if both_pass:
        new_consec = prior_consec + 1
        new_cum = prior_cum + 1
    else:
        new_consec = 0
        new_cum = prior_cum

    # Status: strict decision rule
    paper_sim = _paper_sim_status()
    if new_consec < args.cycles_required:
        status = "watch-pending"
    elif paper_sim["status"] != "ready":
        status = "watch-pending-paper"
    else:
        status = "watch-confirmed"

    btc_obs = _btc_observation()
    hype_obs = _hype_observation()

    cycle_record = WatchCycleRecord(
        cycle_ts_utc=datetime.now(timezone.utc).isoformat(),
        cascade_count=cascade_count,
        cascade_count_delta=cascade_count_delta,
        sol_total_events=len(events),
        sol_h1_30m=_verdict_to_dict(h1_verdicts[30]),
        sol_h1_60m=_verdict_to_dict(h1_verdicts[60]),
        btc_observation=btc_obs,
        hype_observation=hype_obs,
        paper_sim=paper_sim,
        consecutive_passes=new_consec,
        cumulative_passes=new_cum,
        status=status,
        decision_rule={
            "cycles_required": args.cycles_required,
            "paper_sim_min_decisions": args.paper_sim_min_decisions,
            "promotion_n": PROMOTION_N,
            "promotion_pf": PROMOTION_PF,
            "promotion_top_win_share": PROMOTION_TOP_WIN_SHARE,
            "calm_vol_btc_30m": FIXED_CALM_VOL_BTC_30M,
            "calm_vol_eth_30m": FIXED_CALM_VOL_ETH_30M,
        },
    )

    # Append to watch log
    WATCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with WATCH_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(cycle_record)) + "\n")
    print()
    print(f"  BTC observation:  {btc_obs}")
    print(f"  HYPE observation: {hype_obs}")
    print(f"  Paper sim:        {paper_sim}")
    print()
    print(f"  Consecutive passes:  {new_consec} (need {args.cycles_required})")
    print(f"  Cumulative passes:   {new_cum}")
    print(f"  Watch status:        {status}")
    print()
    print(f"  Appended to: {WATCH_LOG_PATH.relative_to(REPO_ROOT)}")
    print()
    print("DONE. Research-only watch. No execution touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Depth / OBI / OFI / stale filter backtest (research only).

Loads the joined L2-cascade feature records (built by
build_l2_cascade_features.py) and tests whether filtering cascades by
their pre-cascade book behavior or post-cascade recovery improves fade
edge beyond the baseline.

Filter buckets tested (per Slim 2026-08-07 Tier-2 shortlist):

  baseline                no L2 filter — control bucket
  obi_5_drop_lt_neg_0_5   book flipped hard in cascade direction (OBI-5
                          moved more than 0.5 in the cascade's direction
                          between t-30s and t+5s)
  obi_10_drop_lt_neg_0_3  same idea at OBI-10, gentler threshold
  depth_top5_thin        book thinned >50% on at least one side
  depth_top10_thin       same at top-10 levels
  spread_widen_gt_0_5    spread blew out >50% from baseline
  ofi_5_30s_neg          strong 30s flow in cascade direction (OFI sign
                          matches cascade side; magnitude large)
  stale_at_t5            stale_book_flag was true at t+5s
  pre_thin_post_recov     book thinned AND recovered >30% by t+60s
                          (depth_top{5,10}_bid_drop<-0.5 AND
                           depth_top5_bid_recovery>0.3 for side=B, mirror
                           for side=A)
  obi_5_recover_gt_0_3    OBI-5 rebalances >0.3 by t+60s

For each (symbol, side, horizon) cell we test the fade rule (opposite
direction of the cascade) and report n, win rate, avg/median pnl, profit
factor, top-win share, and the promotion-gate verdict.

HARD SCOPE: research only. Does NOT touch execution, order_manager,
risk.py, or any live/paper routing. BTC/ETH/SOL only.

Run:
    python scripts/run_depth_obi_filter.py
    python scripts/run_depth_obi_filter.py --horizons 5,15,30,60
    python scripts/run_depth_obi_filter.py --symbol BTC
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.strategy.event_features import _canonical_symbol, _file_stem  # noqa: E402
from src.strategy.fade_or_follow_backtest import (  # noqa: E402
    _bar_dt,
    _continuation_direction,
    _fade_direction,
    _return_pct,
)
from run_fade_or_follow_backtest import _load_candles  # noqa: E402

# --- Symbols (L2 coverage: BTC/ETH/SOL only, per Slim 2026-08-07) ---
SYMBOLS: tuple[str, ...] = ("BTC", "ETH", "SOL")

# --- Forward horizons (minutes) ---
DEFAULT_HORIZONS: tuple[int, ...] = (5, 15, 30, 60)

# --- Round-trip cost (bps) ---
ROUND_TRIP_COST_BPS: float = 8.0

# --- Promotion gate (per Slim's standard) ---
PROMOTION_N: int = 30
PROMOTION_PF: float = 1.5
PROMOTION_TOP_WIN_SHARE: float = 0.35

# --- Paths ---
L2_CASCADE_DIR = REPO_ROOT / "data" / "l2_cascade_features"
CANDLE_DIR = REPO_ROOT / "data" / "ws_candle"
RESULTS_JSON_PATH = REPO_ROOT / "data" / "depth_obi_filter_results.json"
SUMMARY_MD_PATH = REPO_ROOT / "data" / "depth_obi_filter_summary.md"


# ----------------------------------------------------------------------------
# Filter definitions
# ----------------------------------------------------------------------------

# Each filter is a callable (cascade_record) -> bool.
# A cascade is "kept" if the callable returns True.
# Filter thresholds are conservative defaults; tune per regime later.

def _is_baseline(_c: dict) -> bool:
    return True


def _has_l2_coverage(c: dict) -> bool:
    """At least 2 of 4 snapshots present (so pre/post derived features are
    meaningful). Filters with 0 snapshots can't be evaluated."""
    return c.get("_snapshots_present", 0) >= 2


def f_obi_5_drop_lt_neg_0_5(c: dict) -> bool:
    """OBI-5 dropped more than 0.5 in the cascade's direction between
    t-30s and t+5s. Sign convention: for side=B (forced sell) the
    OBI drops toward negative (book flipped bid->ask). For side=A
    (forced buy) the OBI drops toward positive. We use the
    absolute drop in the cascade's expected direction."""
    side = c.get("side")
    pre = c.get("pre_thinning", {}).get("obi_5_drop")
    if pre is None:
        return False
    if side == "B":
        return pre < -0.5
    elif side == "A":
        return pre > 0.5
    return False


def f_obi_10_drop_lt_neg_0_3(c: dict) -> bool:
    side = c.get("side")
    pre = c.get("pre_thinning", {}).get("obi_10_drop")
    if pre is None:
        return False
    if side == "B":
        return pre < -0.3
    elif side == "A":
        return pre > 0.3
    return False


def f_depth_top5_thin(c: dict) -> bool:
    """Top-5 depth dropped >50% on at least one side."""
    pre = c.get("pre_thinning", {})
    bid_drop = pre.get("depth_top5_bid_drop")
    ask_drop = pre.get("depth_top5_ask_drop")
    if bid_drop is None and ask_drop is None:
        return False
    if bid_drop is not None and bid_drop < -0.5:
        return True
    if ask_drop is not None and ask_drop < -0.5:
        return True
    return False


def f_depth_top10_thin(c: dict) -> bool:
    pre = c.get("pre_thinning", {})
    bid_drop = pre.get("depth_top10_bid_drop")
    ask_drop = pre.get("depth_top10_ask_drop")
    if bid_drop is None and ask_drop is None:
        return False
    if bid_drop is not None and bid_drop < -0.5:
        return True
    if ask_drop is not None and ask_drop < -0.5:
        return True
    return False


def f_spread_widen_gt_0_5(c: dict) -> bool:
    pre = c.get("pre_thinning", {}).get("spread_widen")
    if pre is None:
        return False
    return pre > 0.5


def f_ofi_5_30s_neg(c: dict) -> bool:
    """Strong 30s OFI in the cascade's direction. For side=B (forced
    sell), OFI is negative (asks filling). For side=A, OFI is positive.
    Threshold: |OFI-5_30s| > 30 (top-5 notional, symbol-agnostic)."""
    pre = c.get("pre_thinning", {}).get("ofi_5_30s_magnitude")
    if pre is None:
        return False
    side = c.get("side")
    if side == "B":
        return pre < -30.0
    elif side == "A":
        return pre > 30.0
    return False


def f_stale_at_t5(c: dict) -> bool:
    snap = c.get("snapshot_t_plus_5s")
    if not isinstance(snap, dict):
        return False
    return bool(snap.get("stale_book_flag"))


def f_pre_thin_post_recov(c: dict) -> bool:
    """Book thinned >50% on cascade side AND reloaded >30% by t+60s.

    For side=B: bid depth thinned AND bid depth reloaded.
    For side=A: ask depth thinned AND ask depth reloaded.
    """
    side = c.get("side")
    pre = c.get("pre_thinning", {})
    post = c.get("post_resilience", {})
    if side == "B":
        return (
            (pre.get("depth_top5_bid_drop") is not None and pre["depth_top5_bid_drop"] < -0.5)
            and (post.get("depth_top5_bid_recovery") is not None and post["depth_top5_bid_recovery"] > 0.3)
        )
    if side == "A":
        return (
            (pre.get("depth_top5_ask_drop") is not None and pre["depth_top5_ask_drop"] < -0.5)
            and (post.get("depth_top5_ask_recovery") is not None and post["depth_top5_ask_recovery"] > 0.3)
        )
    return False


def f_obi_5_recover_gt_0_3(c: dict) -> bool:
    """OBI-5 rebalances >0.3 by t+60s (absolute rebalance)."""
    post = c.get("post_resilience", {}).get("obi_5_recovery")
    if post is None:
        return False
    return post > 0.3


# Filter registry: name -> (callable, description)
FILTERS: Dict[str, Tuple[Callable[[dict], bool], str]] = {
    "baseline":                (_is_baseline, "no L2 filter (control)"),
    "obi_5_drop_lt_neg_0_5":  (f_obi_5_drop_lt_neg_0_5, "OBI-5 dropped >0.5 in cascade direction (t-30s to t+5s)"),
    "obi_10_drop_lt_neg_0_3": (f_obi_10_drop_lt_neg_0_3, "OBI-10 dropped >0.3 in cascade direction (t-30s to t+5s)"),
    "depth_top5_thin":        (f_depth_top5_thin, "Top-5 depth thinned >50% on at least one side"),
    "depth_top10_thin":       (f_depth_top10_thin, "Top-10 depth thinned >50% on at least one side"),
    "spread_widen_gt_0_5":    (f_spread_widen_gt_0_5, "Spread widened >50% from t-30s baseline"),
    "ofi_5_30s_neg":          (f_ofi_5_30s_neg, "OFI-5 (30s window) magnitude >30 in cascade direction"),
    "stale_at_t5":            (f_stale_at_t5, "stale_book_flag true at t+5s"),
    "pre_thin_post_recov":    (f_pre_thin_post_recov, "depth thin >50% AND reload >30% by t+60s"),
    "obi_5_recover_gt_0_3":   (f_obi_5_recover_gt_0_3, "OBI-5 rebalances >0.3 by t+60s"),
}


# ----------------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------------

@dataclass
class BucketVerdict:
    symbol: str
    side: str
    horizon_minutes: int
    filter_name: str
    filter_description: str
    n: int
    n_total: int  # total cascades in this (symbol, side, horizon) cell
    win_rate: float
    avg_pnl_pct: float
    median_pnl_pct: float
    pf: float
    top_win_share: float
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    passed: bool = False
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------
# L2-cascade loading
# ----------------------------------------------------------------------------

def _load_l2_cascade_records(symbol: str) -> List[dict]:
    """Stream-read all l2_cascade_features records for a symbol across dates."""
    sym_stem = _file_stem(symbol)
    records: List[dict] = []
    for path in sorted(L2_CASCADE_DIR.glob(f"{sym_stem}_*.jsonl")):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("symbol", "").upper() != symbol.upper():
                    continue
                records.append(rec)
    return records


# ----------------------------------------------------------------------------
# Return computation
# ----------------------------------------------------------------------------

def _compute_fade_return(
    cascade: dict,
    candles: List[dict],
    horizon_minutes: int,
) -> Optional[float]:
    """Compute the fade (opposite-direction) return at horizon_minutes.

    Entry price = event_vwap (true cascade VWAP).
    Exit price = close of the bar at or after (start_ts + horizon minutes).
    Side A cascade = ask/sell (forced buy flow) -> fade is short -> -raw
    Side B cascade = bid/buy (forced sell flow) -> fade is long -> +raw

    Returns None if no eligible exit bar.
    Returns are in PERCENT units (e.g. +0.32 = +0.32%, not +32%).
    """
    start_ts = cascade.get("start_ts") or cascade.get("event_ts")
    if not start_ts:
        return None
    entry = cascade.get("event_vwap")
    if entry is None or entry == 0:
        return None
    entry_dt = _bar_dt({"t": _iso_to_ms(start_ts)})
    if entry_dt is None:
        return None
    target_ms = int(entry_dt.timestamp() * 1000) + horizon_minutes * 60_000
    # Find the bar with t >= target_ms.
    chosen = None
    for bar in candles:
        bt = bar.get("t")
        if bt is None:
            continue
        if bt >= target_ms:
            chosen = bar
            break
    if chosen is None:
        return None
    exit_px = chosen.get("c")
    if exit_px is None or exit_px == 0:
        return None
    side = cascade.get("side", "B")
    direction = _fade_direction(side)
    raw = _return_pct(direction, float(entry), float(exit_px))
    # Subtract round-trip cost.
    return raw - ROUND_TRIP_COST_BPS / 100.0


def _iso_to_ms(iso: str) -> int:
    """Parse ISO 8601 to ms since epoch. Naive UTC interpretation."""
    from datetime import datetime, timezone
    try:
        # Strip trailing Z if present.
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------------
# Bucket evaluation
# ----------------------------------------------------------------------------

def _evaluate_bucket(
    records: List[dict],
    candles: List[dict],
    symbol: str,
    side: str,
    horizon: int,
    filter_name: str,
    filter_fn: Callable[[dict], bool],
    filter_desc: str,
) -> BucketVerdict:
    """Run fade backtest on cascades matching the filter, compute verdict."""
    cell = [r for r in records if r.get("side") == side]
    n_total = len(cell)
    matched = [r for r in cell if filter_fn(r)]
    returns: List[float] = []
    for r in matched:
        r_pct = _compute_fade_return(r, candles, horizon)
        if r_pct is None:
            continue
        returns.append(r_pct)
    n = len(returns)
    if n == 0:
        return BucketVerdict(
            symbol=symbol,
            side=side,
            horizon_minutes=horizon,
            filter_name=filter_name,
            filter_description=filter_desc,
            n=0,
            n_total=n_total,
            win_rate=0.0,
            avg_pnl_pct=0.0,
            median_pnl_pct=0.0,
            pf=0.0,
            top_win_share=0.0,
            passed=False,
            reason="no trades (no candles or no L2 coverage)",
        )
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = -sum(r for r in returns if r < 0)  # positive number
    avg = sum(returns) / n
    median = statistics.median(returns)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    top_win = max(returns) if returns else 0.0
    top_win_share = top_win / gross_profit if gross_profit > 0 else 0.0
    win_rate = len(wins) / n
    passed, reason = _verdict(n, pf, top_win_share)
    return BucketVerdict(
        symbol=symbol,
        side=side,
        horizon_minutes=horizon,
        filter_name=filter_name,
        filter_description=filter_desc,
        n=n,
        n_total=n_total,
        win_rate=win_rate,
        avg_pnl_pct=avg,
        median_pnl_pct=median,
        pf=pf,
        top_win_share=top_win_share,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        passed=passed,
        reason=reason,
    )


def _verdict(n: int, pf: float, top_win_share: float) -> Tuple[bool, str]:
    if n < PROMOTION_N:
        return False, f"n={n} < {PROMOTION_N}"
    if not math.isfinite(pf):
        # No losses at all — flagged as suspicious until we have more data.
        return False, f"PF=inf (no losses; suspicious, n={n})"
    if pf < PROMOTION_PF:
        return False, f"PF={pf:.2f} < {PROMOTION_PF}"
    if top_win_share > PROMOTION_TOP_WIN_SHARE:
        return False, f"top_win_share={top_win_share:.2f} > {PROMOTION_TOP_WIN_SHARE}"
    return True, "PASS"


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def _print_report(verdicts: List[BucketVerdict]) -> None:
    print()
    print("=" * 96)
    print("DEPTH/OBI/OFI FILTER BACKTEST — Tier-2 L2 features")
    print("=" * 96)
    by_filter: Dict[str, List[BucketVerdict]] = defaultdict(list)
    for v in verdicts:
        by_filter[v.filter_name].append(v)
    header = (
        f"  {'filter':<24} {'sym':<5} {'side':<4} {'horiz':>5} {'n':>4} {'/total':>7} "
        f"{'WR%':>6} {'avg%':>8} {'med%':>8} {'PF':>7} {'topWin%':>8}  {'verdict'}"
    )
    print(header)
    print("  " + "-" * 92)
    for fname in ("baseline", "obi_5_drop_lt_neg_0_5", "obi_10_drop_lt_neg_0_3",
                  "depth_top5_thin", "depth_top10_thin", "spread_widen_gt_0_5",
                  "ofi_5_30s_neg", "stale_at_t5", "pre_thin_post_recov",
                  "obi_5_recover_gt_0_3"):
        rows = by_filter.get(fname, [])
        if not rows:
            continue
        for v in sorted(rows, key=lambda x: (x.symbol, x.side, x.horizon_minutes)):
            pf_str = f"{v.pf:>6.2f}" if math.isfinite(v.pf) else "   inf"
            verdict = "PASS" if v.passed else f"  ({v.reason})"
            print(
                f"  {v.filter_name:<24} {v.symbol:<5} {v.side:<4} {v.horizon_minutes:>5} "
                f"{v.n:>4} {v.n_total:>7} {v.win_rate*100:>5.1f}% {v.avg_pnl_pct:>+7.4f} "
                f"{v.median_pnl_pct:>+7.4f} {pf_str:>7} {v.top_win_share*100:>6.1f}%  {verdict}"
            )
    print()
    n_passed = sum(1 for v in verdicts if v.passed)
    n_evaluated = sum(1 for v in verdicts if v.n > 0)
    print(f"SUMMARY: {n_passed}/{n_evaluated} non-empty buckets passed the promotion gate.")
    print()
    print("INTERPRETATION GUIDE")
    print("-" * 96)
    print("  baseline: the unfiltered fade rule (control).")
    print("  A filter is a candidate if it INCREASES PF / avg / med vs baseline")
    print("  AT THE SAME horizon, on the SAME (sym, side). Absolute PF > 1.5 alone")
    print("  is not enough — we need the filter to add edge over the baseline.")
    print("  pre_thin_post_recov is the most specific filter: book thinned AND")
    print("  reloaded. If this passes, the cascade's book behavior predicted the")
    print("  fade. If baseline and pre_thin_post_recov are similar, the L2 features")
    print("  add no information beyond what the cascade already implies.")
    print()


def _write_summary_md(verdicts: List[BucketVerdict], path: Path) -> None:
    lines: List[str] = []
    lines.append("# Depth / OBI / OFI Filter Backtest")
    lines.append("")
    lines.append("Tier-2 L2 features joined with cascade events. Buckets tested per "
                 "Slim's 2026-08-07 Tier-2 shortlist.")
    lines.append("")
    lines.append("Promotion gate: n >= 30, PF > 1.5, top_win_share <= 0.35.")
    lines.append("")
    by_filter: Dict[str, List[BucketVerdict]] = defaultdict(list)
    for v in verdicts:
        by_filter[v.filter_name].append(v)
    lines.append("| filter | symbol | side | horizon | n | n_total | WR% | avg% | med% | PF | top_win% | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for fname in FILTERS.keys():
        rows = by_filter.get(fname, [])
        if not rows:
            continue
        for v in sorted(rows, key=lambda x: (x.symbol, x.side, x.horizon_minutes)):
            pf_str = f"{v.pf:.2f}" if math.isfinite(v.pf) else "inf"
            verdict = "**PASS**" if v.passed else v.reason
            lines.append(
                f"| {v.filter_name} | {v.symbol} | {v.side} | {v.horizon_minutes} | "
                f"{v.n} | {v.n_total} | {v.win_rate*100:.1f} | {v.avg_pnl_pct:+.4f} | "
                f"{v.median_pnl_pct:+.4f} | {pf_str} | {v.top_win_share*100:.1f} | {verdict} |"
            )
    n_passed = sum(1 for v in verdicts if v.passed)
    n_evaluated = sum(1 for v in verdicts if v.n > 0)
    lines.append("")
    lines.append(f"**Summary:** {n_passed}/{n_evaluated} non-empty buckets passed the gate.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest L2-driven filter buckets on cascade fades (research only)."
    )
    parser.add_argument(
        "--symbol", nargs="+", default=None,
        help="Symbols to test (uppercase). Default: BTC ETH SOL"
    )
    parser.add_argument(
        "--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS),
        help="Comma-separated horizons in minutes (default: 5,15,30,60)"
    )
    args = parser.parse_args(argv)

    symbols: Tuple[str, ...] = tuple(s.upper() for s in (args.symbol or SYMBOLS))
    horizons: Tuple[int, ...] = tuple(int(h) for h in args.horizons.split(","))
    print(f"symbols={symbols} horizons={horizons}")

    verdicts: List[BucketVerdict] = []
    for sym in symbols:
        records = _load_l2_cascade_records(sym)
        if not records:
            print(f"[{sym}] no l2_cascade_features records found, skipping")
            continue
        candles = _load_candles(sym, CANDLE_DIR)
        if not candles:
            print(f"[{sym}] no candle data, skipping")
            continue
        # Count sides for the report.
        side_counts: Dict[str, int] = defaultdict(int)
        for r in records:
            side_counts[r.get("side", "?")] += 1
        print(f"[{sym}] {len(records)} records (A={side_counts.get('A', 0)}, "
              f"B={side_counts.get('B', 0)}), {len(candles)} candles")
        for side in ("A", "B"):
            for horizon in horizons:
                for fname, (fn, desc) in FILTERS.items():
                    v = _evaluate_bucket(
                        records, candles, sym, side, horizon, fname, fn, desc
                    )
                    verdicts.append(v)

    _print_report(verdicts)

    RESULTS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON_PATH.write_text(
        json.dumps([v.to_dict() for v in verdicts], indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(verdicts)} verdicts to {RESULTS_JSON_PATH.name}")

    _write_summary_md(verdicts, SUMMARY_MD_PATH)
    print(f"Wrote summary to {SUMMARY_MD_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

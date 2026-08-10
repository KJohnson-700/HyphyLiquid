"""ETH side=A follow 60m funding_z=funding_pos_elevated paper/canary lane (research only).

Per Slim 2026-08-09 directive: Codex picked this bucket as the v1 paper candidate.
This script wires the bucket as a separate tagged canary lane:
  - Symbol:   ETH only
  - Side:     A (ask / forced buy flow / cascade up)
  - Horizon:  60m
  - Filter:   funding_z in [1.0, 2.0) at cascade time (the established
              "funding_pos_elevated" bucket from run_context_filter_backtest)
  - Action:   follow (continuation of the cascade direction). For side=A
              the follow direction is "short" (bet the price keeps falling
              after the forced-buy cascade exhausts).

The lane is RESEARCH ONLY and does not:
  - Touch risk.py
  - Touch order_manager
  - Touch live execution
  - Replace or modify any existing paper_trade_loop output

Outputs are tagged with lane="eth_follow_canary" so they can be isolated
from the existing paper/research lanes.

Outputs:
  - data/eth_follow_canary_decisions.jsonl   (one record per matching cascade)
  - data/eth_follow_canary_results.json      (summary metrics + promotion gate)
  - data/eth_follow_canary_summary.md        (markdown report)

The funding_z math (240-min rolling Z-score, 30-min min history, population
stdev) is inlined here for self-containment; it matches the established
implementation in scripts/run_context_filter_backtest.py.

Usage:
    python scripts/run_eth_follow_canary.py
    python scripts/run_eth_follow_canary.py --horizons 30,60,120
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
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from src.strategy.event_features import _canonical_symbol, _file_stem  # noqa: E402
from src.strategy.fade_or_follow_backtest import (  # noqa: E402
    _continuation_direction,
    _return_pct,
)
from run_fade_or_follow_backtest import _load_candles  # noqa: E402

# --- Lane identity (Slim 2026-08-09 directive) ---
LANE_NAME: str = "eth_follow_canary"
SYMBOL: str = "ETH"
SIDE: str = "A"
DEFAULT_HORIZONS: Tuple[int, ...] = (60,)
ROUND_TRIP_COST_BPS: float = 8.0

# --- Funding-Z math (matches run_context_filter_backtest) ---
FUNDING_Z_LOOKBACK: int = 240      # minutes of rolling history
FUNDING_Z_MIN_HISTORY: int = 30    # minimum samples required
FUNDING_POS_ELEVATED_LO: float = 1.0   # z >= this = funding_pos_elevated
FUNDING_POS_ELEVATED_HI: float = 2.0   # z <  this  (else "funding_pos_extreme")

# --- Promotion gate (same as the rest of the backtest rig) ---
PROMOTION_N: int = 30
PROMOTION_PF: float = 1.5
PROMOTION_MEDIAN: float = 0.0
PROMOTION_TOP_WIN_SHARE: float = 0.35

# --- Paths ---
CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
ASSET_CTX_DIR = REPO_ROOT / "data" / "asset_ctx"
CANDLE_DIR = REPO_ROOT / "data" / "ws_candle"
DECISIONS_PATH = REPO_ROOT / "data" / "eth_follow_canary_decisions.jsonl"
RESULTS_PATH = REPO_ROOT / "data" / "eth_follow_canary_results.json"
SUMMARY_PATH = REPO_ROOT / "data" / "eth_follow_canary_summary.md"


# ----------------------------------------------------------------------------
# Funding-Z (inlined from run_context_filter_backtest for self-containment)
# ----------------------------------------------------------------------------

def _funding_z_score(
    rows: List[dict],
    idx: int,
    *,
    lookback: int = FUNDING_Z_LOOKBACK,
    min_history: int = FUNDING_Z_MIN_HISTORY,
) -> Optional[float]:
    """Rolling Z-score of the funding rate at index `idx`.

    History = up to `lookback` rows immediately before `idx`. Requires
    `min_history` samples with non-null funding to return a non-null Z.
    Uses population stdev (matches numpy default; matches the established
    context_filter implementation).
    """
    current = rows[idx].get("funding")
    if current is None:
        return None
    start = max(0, idx - lookback)
    history = [float(r["funding"]) for r in rows[start:idx] if r.get("funding") is not None]
    if len(history) < min_history:
        return None
    stdev = statistics.pstdev(history)
    if stdev <= 0:
        return 0.0
    return (float(current) - statistics.mean(history)) / stdev


def _is_funding_pos_elevated(z: Optional[float]) -> bool:
    if z is None:
        return False
    return FUNDING_POS_ELEVATED_LO <= z < FUNDING_POS_ELEVATED_HI


# ----------------------------------------------------------------------------
# Asset-ctx + cascade loaders
# ----------------------------------------------------------------------------

def _parse_ts_ms(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _parse_ctx_row(row: dict) -> Optional[dict]:
    """Parse one asset_ctx row -> {ts_ms, funding, oi, mark}."""
    ctx = row.get("context") if isinstance(row, dict) else None
    if not isinstance(ctx, dict):
        return None
    ts = _parse_ts_ms(str(row.get("poll_ts", "")))
    if ts is None:
        return None
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    funding = _f(ctx.get("funding"))
    oi = _f(ctx.get("openInterest"))
    mark = _f(ctx.get("markPx") or ctx.get("midPx") or ctx.get("oraclePx"))
    if funding is None and oi is None and mark is None:
        return None
    return {"ts_ms": ts, "funding": funding, "oi": oi, "mark": mark}


def _load_asset_ctx(symbol: str) -> List[dict]:
    rows: List[dict] = []
    stem = _file_stem(symbol)
    for path in sorted(ASSET_CTX_DIR.glob(f"{stem}_*.jsonl")):
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
                parsed = _parse_ctx_row(rec)
                if parsed is not None:
                    rows.append(parsed)
    rows.sort(key=lambda r: r["ts_ms"])
    return rows


def _row_at_or_before(rows: List[dict], ts_ms: int) -> Optional[int]:
    """Binary search: index of the last row with ts_ms <= target, or None."""
    if not rows:
        return None
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid]["ts_ms"] <= ts_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo > 0 else None


def _load_cascades_for_lane(symbol: str, side: str) -> List[dict]:
    """Read cascades.jsonl, filter to {symbol, side}, return sorted by ts."""
    if not CASCADES_PATH.exists():
        return []
    out: List[dict] = []
    with CASCADES_PATH.open("r", encoding="utf-8") as f:
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
            if rec.get("side", "").upper() != side.upper():
                continue
            ts_ms = rec.get("event_ts_ms") or _parse_ts_ms(str(rec.get("event_ts", "")))
            if ts_ms is None:
                continue
            rec["_lane_ts_ms"] = int(ts_ms)
            out.append(rec)
    out.sort(key=lambda r: r["_lane_ts_ms"])
    return out


# ----------------------------------------------------------------------------
# Per-cascade decision
# ----------------------------------------------------------------------------

@dataclass
class CanaryDecision:
    """One row in data/eth_follow_canary_decisions.jsonl."""
    lane: str
    symbol: str
    side: str
    event_ts_ms: int
    event_ts: str
    event_vwap: Optional[float]
    total_notional: Optional[float]
    n_fills: Optional[int]
    funding: Optional[float]
    funding_z: Optional[float]
    funding_z_bucket: str
    matched_filter: bool
    horizon_minutes: int
    direction: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    return_pct: Optional[float]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _bucket_name(z: Optional[float]) -> str:
    if z is None:
        return "funding_unknown"
    if z >= 2.0:
        return "funding_pos_extreme"
    if z <= -2.0:
        return "funding_neg_extreme"
    if z >= 1.0:
        return "funding_pos_elevated"
    if z <= -1.0:
        return "funding_neg_elevated"
    return "funding_normal"


def _candle_close_at_or_after(candles: List[dict], candle_ts: List[int], ts_ms: int) -> Optional[Tuple[int, float]]:
    """Return (idx, close) of the first candle with t >= ts_ms, or None."""
    if not candles:
        return None
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candle_ts[mid] < ts_ms:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(candles):
        return None
    bar = candles[lo]
    close = bar.get("c")
    if close is None and isinstance(bar.get("payload"), dict):
        close = bar["payload"].get("c")
    if close is None:
        return None
    try:
        return lo, float(close)
    except (TypeError, ValueError):
        return None


def _build_decision(
    cascade: dict,
    ctx_rows: List[dict],
    candles: List[dict],
    candle_ts: List[int],
    horizon: int,
) -> CanaryDecision:
    ts_ms = int(cascade["_lane_ts_ms"])
    sym = cascade.get("symbol", SYMBOL)
    side = cascade.get("side", SIDE)
    idx = _row_at_or_before(ctx_rows, ts_ms)
    funding = None
    funding_z = None
    if idx is not None:
        funding = ctx_rows[idx].get("funding")
        funding_z = _funding_z_score(ctx_rows, idx)
    bucket = _bucket_name(funding_z)
    matched = _is_funding_pos_elevated(funding_z)

    direction = _continuation_direction(side)  # side=A -> "short"
    entry_price = None
    exit_price = None
    return_pct = None
    reason = "filter: not funding_pos_elevated"
    if matched:
        # Entry: first candle with t >= event_ts.
        entry = _candle_close_at_or_after(candles, candle_ts, ts_ms)
        if entry is None:
            reason = "no entry candle"
        else:
            entry_idx, entry_price = entry
            exit_idx = entry_idx + horizon
            if exit_idx >= len(candles):
                reason = f"no exit candle at +{horizon}m"
            else:
                bar = candles[exit_idx]
                exit_close = bar.get("c")
                if exit_close is None and isinstance(bar.get("payload"), dict):
                    exit_close = bar["payload"].get("c")
                if exit_close is None or float(exit_close) <= 0:
                    reason = "exit close missing or zero"
                else:
                    exit_price = float(exit_close)
                    raw = _return_pct(direction, float(entry_price), float(exit_price))
                    return_pct = round(raw - ROUND_TRIP_COST_BPS / 100.0, 4)
                    reason = f"follow 60m funding_pos_elevated, return computed"

    return CanaryDecision(
        lane=LANE_NAME,
        symbol=sym,
        side=side,
        event_ts_ms=ts_ms,
        event_ts=str(cascade.get("event_ts", "")),
        event_vwap=cascade.get("event_vwap"),
        total_notional=cascade.get("total_notional"),
        n_fills=cascade.get("n_fills"),
        funding=funding,
        funding_z=funding_z,
        funding_z_bucket=bucket,
        matched_filter=matched,
        horizon_minutes=horizon,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=return_pct,
        reason=reason,
    )


# ----------------------------------------------------------------------------
# Aggregation + promotion gate
# ----------------------------------------------------------------------------

@dataclass
class LaneVerdict:
    symbol: str
    side: str
    horizon_minutes: int
    filter_name: str
    n_total: int          # ETH side=A cascades evaluated
    n_matched: int        # those passing funding_pos_elevated
    n_evaluated: int      # matched with valid return computed
    n_skipped: int        # matched but no candle / no exit
    win_rate: float
    avg_pnl_pct: float
    median_pnl_pct: float
    pf: float
    top_win_share: float
    gross_profit: float
    gross_loss: float
    passed: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _profit_factor(values: List[float]) -> Tuple[float, float, float]:
    gross_profit = sum(v for v in values if v > 0)
    gross_loss = abs(sum(v for v in values if v < 0))
    if gross_loss == 0:
        pf = float("inf") if gross_profit > 0 else 0.0
    else:
        pf = gross_profit / gross_loss
    return pf, gross_profit, gross_loss


def _top_win_share(values: List[float], gross_profit: float) -> float:
    wins = [v for v in values if v > 0]
    if not wins or gross_profit <= 0:
        return 0.0
    return max(wins) / gross_profit


def _verdict(verdict: LaneVerdict) -> LaneVerdict:
    reasons: List[str] = []
    if verdict.n_evaluated < PROMOTION_N:
        reasons.append(f"n<{PROMOTION_N}")
    if not (verdict.pf > PROMOTION_PF and verdict.pf != float("inf")):
        # Inf PF (no losses) is suspicious until n is large.
        if verdict.pf == float("inf"):
            reasons.append("pf=inf (no losses; suspicious)")
        else:
            reasons.append(f"pf<={PROMOTION_PF}")
    if verdict.median_pnl_pct <= PROMOTION_MEDIAN:
        reasons.append("median<=0")
    if verdict.top_win_share > PROMOTION_TOP_WIN_SHARE:
        reasons.append(f"top_win_share>{PROMOTION_TOP_WIN_SHARE}")
    verdict.passed = not reasons
    verdict.reason = "pass" if verdict.passed else ", ".join(reasons)
    return verdict


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def _print_report(verdict: LaneVerdict) -> None:
    print()
    print("=" * 78)
    print("ETH side=A FOLLOW 60m CANARY — funding_pos_elevated (lane: eth_follow_canary)")
    print("=" * 78)
    print(f"  Total ETH side=A cascades:    {verdict.n_total}")
    print(f"  Matched funding_pos_elevated: {verdict.n_matched}")
    print(f"  Evaluated (return computed):  {verdict.n_evaluated}")
    print(f"  Skipped (no candle / exit):   {verdict.n_skipped}")
    print()
    print(f"  WR%:     {verdict.win_rate*100:>6.2f}")
    print(f"  avg%:    {verdict.avg_pnl_pct:>+8.4f}")
    print(f"  med%:    {verdict.median_pnl_pct:>+8.4f}")
    pf_str = f"{verdict.pf:>6.2f}" if verdict.pf != float("inf") else "   inf"
    print(f"  PF:      {pf_str}")
    print(f"  topWin%: {verdict.top_win_share*100:>6.2f}")
    print(f"  gross+:  {verdict.gross_profit:>+8.4f}")
    print(f"  gross-:  {-verdict.gross_loss:>+8.4f}")
    print()
    if verdict.passed:
        print("  VERDICT: PASS (meets promotion gate)")
    else:
        print(f"  VERDICT: HOLD ({verdict.reason})")
    print()
    print("INTERPRETATION GUIDE")
    print("-" * 78)
    print("  Lane: ETH side=A follow 60m, filter funding_z in [1.0, 2.0).")
    print("  Direction: short (follow the cascade — bet the price keeps falling).")
    print("  Bucket source: Tier-1 context filter (commit b4e9e41, 2026-08-07).")
    print("  Output is TAGGED with lane='eth_follow_canary' so it does not")
    print("  pollute the existing paper/research lanes.")
    print("  Promotion gate: n>=30, PF>1.5, median>0, top_win_share<=0.35.")
    print("  Inf PF (no losses) is rejected as suspicious until n is large.")
    print()


def _write_summary_md(verdict: LaneVerdict, path: Path) -> None:
    pf_str = f"{verdict.pf:.2f}" if verdict.pf != float("inf") else "inf"
    verdict_str = "**PASS**" if verdict.passed else f"HOLD ({verdict.reason})"
    body = f"""# ETH side=A Follow 60m Canary — funding_pos_elevated

Lane: `{LANE_NAME}` (research only). Tagged separate from existing paper
lanes; does not touch risk.py, order_manager, or live execution.

## Filter
- Symbol: ETH
- Side: A (ask / forced buy flow / cascade up)
- Horizon: 60m
- Funding-Z bucket: `funding_pos_elevated` (z in [1.0, 2.0))
- Action: follow = `short` (bet the price continues down after the
  forced-buy cascade exhausts)

## Counts
- Total ETH side=A cascades: {verdict.n_total}
- Matched funding_pos_elevated: {verdict.n_matched}
- Evaluated (return computed): {verdict.n_evaluated}
- Skipped (no candle / exit): {verdict.n_skipped}

## Metrics
| metric | value |
|---|---|
| win_rate | {verdict.win_rate*100:.2f}% |
| avg_pnl_pct | {verdict.avg_pnl_pct:+.4f} |
| median_pnl_pct | {verdict.median_pnl_pct:+.4f} |
| profit_factor | {pf_str} |
| top_win_share | {verdict.top_win_share*100:.2f}% |
| gross_profit | {verdict.gross_profit:+.4f} |
| gross_loss | {-verdict.gross_loss:+.4f} |

## Verdict: {verdict_str}

Promotion gate: n >= {PROMOTION_N}, PF > {PROMOTION_PF}, median > {PROMOTION_MEDIAN},
top_win_share <= {PROMOTION_TOP_WIN_SHARE}. Inf PF (no losses) is rejected
as suspicious until n is large.
"""
    path.write_text(body, encoding="utf-8")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ETH side=A follow 60m funding_pos_elevated canary lane (research only)."
    )
    parser.add_argument(
        "--horizons", default="60",
        help=f"Comma-separated horizons (default: 60). Current lane is single-horizon.",
    )
    args = parser.parse_args(argv)
    horizons = tuple(int(h) for h in args.horizons.split(","))
    if horizons != (60,):
        print(f"NOTE: lane is locked to 60m; ignoring --horizons={horizons}, using (60,)")
        horizons = (60,)

    print(f"lane={LANE_NAME}  symbol={SYMBOL}  side={SIDE}  horizon={horizons[0]}m")
    print(f"filter: funding_z in [{FUNDING_POS_ELEVATED_LO}, {FUNDING_POS_ELEVATED_HI})")
    print()

    cascades = _load_cascades_for_lane(SYMBOL, SIDE)
    print(f"loaded {len(cascades)} {SYMBOL} side={SIDE} cascades")
    if not cascades:
        print("no cascades; nothing to evaluate")
        return 0

    ctx_rows = _load_asset_ctx(SYMBOL)
    print(f"loaded {len(ctx_rows)} {SYMBOL} asset_ctx rows")

    candles = _load_candles(SYMBOL, CANDLE_DIR)
    candle_ts = [int(c.get("t") or c.get("payload", {}).get("t") or 0) for c in candles]
    print(f"loaded {len(candles)} {SYMBOL} candles")

    horizon = horizons[0]
    decisions: List[CanaryDecision] = []
    for c in cascades:
        d = _build_decision(c, ctx_rows, candles, candle_ts, horizon)
        decisions.append(d)

    n_total = len(decisions)
    n_matched = sum(1 for d in decisions if d.matched_filter)
    matched_with_return = [d for d in decisions if d.matched_filter and d.return_pct is not None]
    n_evaluated = len(matched_with_return)
    n_skipped = n_matched - n_evaluated
    returns = [d.return_pct for d in matched_with_return]  # type: ignore[arg-type]

    if returns:
        wins = [v for v in returns if v > 0]
        pf, gross_profit, gross_loss = _profit_factor(returns)
        verdict = LaneVerdict(
            symbol=SYMBOL,
            side=SIDE,
            horizon_minutes=horizon,
            filter_name="funding_z=funding_pos_elevated",
            n_total=n_total,
            n_matched=n_matched,
            n_evaluated=n_evaluated,
            n_skipped=n_skipped,
            win_rate=round(len(wins) / len(returns), 4),
            avg_pnl_pct=round(sum(returns) / len(returns), 4),
            median_pnl_pct=round(statistics.median(returns), 4),
            pf=pf,
            top_win_share=round(_top_win_share(returns, gross_profit), 4),
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            passed=False,
            reason="",
        )
    else:
        verdict = LaneVerdict(
            symbol=SYMBOL,
            side=SIDE,
            horizon_minutes=horizon,
            filter_name="funding_z=funding_pos_elevated",
            n_total=n_total,
            n_matched=n_matched,
            n_evaluated=0,
            n_skipped=n_skipped,
            win_rate=0.0,
            avg_pnl_pct=0.0,
            median_pnl_pct=0.0,
            pf=0.0,
            top_win_share=0.0,
            gross_profit=0.0,
            gross_loss=0.0,
            passed=False,
            reason="no matched cascades with valid return",
        )
    verdict = _verdict(verdict)

    _print_report(verdict)

    # Write outputs (atomic temp + rename for jsonl).
    DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DECISIONS_PATH.with_suffix(DECISIONS_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d.to_dict()) + "\n")
    tmp.replace(DECISIONS_PATH)
    print(f"wrote {len(decisions)} decisions to {DECISIONS_PATH.name}")

    RESULTS_PATH.write_text(
        json.dumps(verdict.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote verdict to {RESULTS_PATH.name}")

    _write_summary_md(verdict, SUMMARY_PATH)
    print(f"wrote summary to {SUMMARY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

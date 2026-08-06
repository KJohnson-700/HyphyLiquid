"""BTC ask-heavy x failed-reclaim join backtest (research only).

Per Slim 2026-08-06: BTC ask-heavy needs a more specific failed-reclaim
join, not generic book persistence. This script tests the JOIN of two
pre-existing signals:

  1. BTC B-side cascade at event time has BBO ask_heavy
     (top_book_imbalance < 0.45 -- pre-cascade book is ask-heavy, i.e.
     bids are thin and asks dominate, so the cascade is hitting into a
     wall of resting ask liquidity at the top of book -- after the
     cascade this becomes a continuation tailwind, not a headwind).
  2. Standard failed_reclaim_continuation rule (from
     src.strategy.fade_or_follow_backtest): wait `wait_minutes` for a
     reclaim (close back through event_vwap in the inverse direction);
     if no reclaim, enter WITH the cascade and hold for `horizon_minutes`.

The hypothesis: ask-heavy pre-cascade book + no-reclaim = continuation
edge is stronger than the generic failed_reclaim_continuation bucket
which currently decayed (BTC B PF 1.04 / n=339 baseline, 0.89 / n=105
failed_reclaim_continuation).

We also test the inverse filter (bid_heavy pre-cascade) and a couple of
sister buckets (ask_heavy + always fade, ask_heavy + always follow) so
the join's added value is visible against the 1-D filter alternatives.

HARD SCOPE: research only. Does NOT touch execution, order_manager,
risk.py, or any live/paper routing. BTC/ETH only (v1 symbols).

Run:
    python scripts/run_btc_ask_heavy_reclaim_join.py
    python scripts/run_btc_ask_heavy_reclaim_join.py --horizon 15 --wait 3
    python scripts/run_btc_ask_heavy_reclaim_join.py --ask-heavy-threshold 0.45
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.strategy.fade_or_follow_backtest import (  # noqa: E402
    _bar_dt,
    _continuation_direction,
    _fade_direction,
    _is_reclaim,
    _return_pct,
    find_entry_idx,
)


# ----------------------------- constants ---------------------------------- #

# Per Slim 2026-08-06: BTC ask-heavy needs a more specific failed-reclaim
# join. We focus on the v1 trade symbols (BTC, ETH) for research.
SYMBOLS: tuple[str, ...] = ("BTC", "ETH")

# Backtest parameters (defaults match the v1 spec: horizon=15, wait=3).
DEFAULT_HORIZON: int = 15
DEFAULT_WAIT: int = 3
DEFAULT_MAX_ENTRY_LAG_MINUTES: int = 2

# BBO imbalance buckets (must match the rest of the backtest rig).
# top_book_imbalance is bid / (bid + ask); > 0.55 = bid_heavy,
# < 0.45 = ask_heavy, else balanced.
ASK_HEAVY_THRESHOLD: float = 0.45  # imbalance < this is ask_heavy
BID_HEAVY_THRESHOLD: float = 0.55  # imbalance > this is bid_heavy

# Round-trip cost in bps (paper-loop default; matches the rest of the rig).
ROUND_TRIP_COST_BPS: float = 8.0
STOP_SLIPPAGE_BPS: float = 2.0

# Promotion gate (same as the rest of the backtest rig).
PROMOTION_N: int = 30
PROMOTION_PF: float = 1.5
PROMOTION_TOP_WIN_SHARE: float = 0.35

# Paths.
CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
CANDLES_DIR = REPO_ROOT / "data" / "ws_candle"
RESULTS_JSON_PATH = REPO_ROOT / "data" / "btc_ask_heavy_reclaim_join_results.json"
SUMMARY_MD_PATH = REPO_ROOT / "data" / "btc_ask_heavy_reclaim_join_summary.md"


# ----------------------------- dataclasses ------------------------------- #


@dataclass
class BucketVerdict:
    symbol: str
    side: str
    bucket: str  # join label, e.g. "ask_heavy_AND_failed_reclaim_continuation"
    n: int
    win_rate: float
    avg_pnl_pct: float
    median_pnl_pct: float
    pf: float
    top_win_share: float
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    passed: bool = False
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------- data loaders --------------------------------- #


def _load_cascades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _file_stem(symbol: str) -> str:
    if ":" not in symbol:
        return symbol.lower()
    dex, market = symbol.split(":", 1)
    return f"{dex.lower()}_{market.lower()}"


def _load_candles(symbol: str) -> list[dict]:
    """Load final 1m candle records for symbol across all collected dates.

    Mirrors scripts/run_fade_or_follow_backtest._load_candles so the math
    is consistent with the v1 baseline. Each (date, open_ts) keeps the last
    ws_candle update, which is the candle's final state.
    """
    canonical = symbol.upper() if ":" not in symbol else symbol
    paths = sorted(CANDLES_DIR.glob(f"{_file_stem(canonical)}_*.jsonl"))
    if not paths:
        return []
    by_open: dict[int, dict] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if not isinstance(payload, dict):
                continue
            t = payload.get("t")
            c = payload.get("c")
            if t is None or c is None:
                continue
            try:
                by_open[int(t)] = {
                    "t": int(t),
                    "c": float(c),
                    "o": float(payload.get("o", 0)),
                    "h": float(payload.get("h", 0)),
                    "l": float(payload.get("l", 0)),
                    "v": float(payload.get("v", 0)),
                    "n": int(payload.get("n", 0)),
                }
            except (TypeError, ValueError):
                continue
    return [by_open[t] for t in sorted(by_open)]


def _close(bar: dict) -> float | None:
    try:
        return float(bar.get("c") or bar.get("payload", {}).get("c"))
    except (TypeError, ValueError):
        return None


# --------------------------- imbalance helpers --------------------------- #


def _bbo_bucket(imbalance: float | None) -> str:
    if imbalance is None:
        return "unknown"
    if imbalance > BID_HEAVY_THRESHOLD:
        return "bid_heavy"
    if imbalance < ASK_HEAVY_THRESHOLD:
        return "ask_heavy"
    return "balanced"


# --------------------------- core per-event math -------------------------- #


def _reclaim_detected(
    side: str,
    event_vwap: float,
    candles: list[dict],
    entry_idx: int,
    wait_minutes: int,
) -> bool:
    """Standard reclaim check: any 1m bar close in [entry, entry+wait]
    satisfies the inverse direction (B-side: close<vwap, A-side: close>vwap)."""
    window_end = min(entry_idx + wait_minutes, len(candles) - 1)
    for j in range(entry_idx, window_end + 1):
        c = _close(candles[j])
        if c is None or c <= 0:
            continue
        if _is_reclaim(side, c, event_vwap):
            return True
    return False


def _entry_px(candles: list[dict], entry_idx: int) -> float | None:
    if not candles or entry_idx < 0 or entry_idx >= len(candles):
        return None
    c = _close(candles[entry_idx])
    if c is None or c <= 0:
        return None
    return c


def _exit_px(candles: list[dict], entry_idx: int, horizon: int) -> tuple[float, int] | None:
    if not candles or entry_idx < 0 or entry_idx >= len(candles):
        return None
    ex_idx = entry_idx + horizon
    if ex_idx >= len(candles):
        return None
    c = _close(candles[ex_idx])
    if c is None or c <= 0:
        return None
    return c, ex_idx


def _trade_record(
    *,
    cascade: dict,
    symbol: str,
    side: str,
    direction: str,
    entry_idx: int,
    exit_idx: int,
    entry_px: float,
    exit_px: float,
    bucket: str,
    reclaimed: bool,
    bbo_bucket: str,
) -> dict:
    raw_pnl_pct = _return_pct(direction, entry_px, exit_px)
    # Subtract round-trip + slippage in bps; convert to percent.
    cost_pct = (ROUND_TRIP_COST_BPS + STOP_SLIPPAGE_BPS) / 100.0
    if direction == "long":
        net_pnl_pct = raw_pnl_pct - cost_pct
    else:
        net_pnl_pct = raw_pnl_pct - cost_pct
    return {
        "symbol": symbol,
        "side": side,
        "direction": direction,
        "bucket": bucket,
        "bbo_bucket_at_event": bbo_bucket,
        "reclaim_detected": reclaimed,
        "entry_idx": int(entry_idx),
        "exit_idx": int(exit_idx),
        "entry_px": round(entry_px, 8),
        "exit_px": round(exit_px, 8),
        "raw_pnl_pct": round(raw_pnl_pct, 4),
        "net_pnl_pct": round(net_pnl_pct, 4),
        "event_vwap": float(cascade.get("event_vwap") or 0.0),
        "top_book_imbalance": cascade.get("top_book_imbalance"),
        "start_ts": cascade.get("start_ts"),
    }


# --------------------------- bucket builders ------------------------------ #


@dataclass
class _BucketAgg:
    """Mutable per-bucket aggregation."""

    trades: list[dict] = field(default_factory=list)
    wins: list[float] = field(default_factory=list)  # raw $ values for PF
    losses: list[float] = field(default_factory=list)
    n: int = 0
    n_imb: int = 0  # cascades that had imbalance data

    def add(self, trade: dict) -> None:
        self.trades.append(trade)
        self.n += 1
        # PF uses raw dollar PnL (net_pnl_pct is in percent units; we
        # treat as proxy dollars for ratio consistency).
        net = float(trade.get("net_pnl_pct", 0.0))
        if net > 0:
            self.wins.append(net)
        elif net < 0:
            self.losses.append(-net)  # store losses as positive

    def summary(self, symbol: str, side: str, bucket: str) -> BucketVerdict:
        if self.n == 0:
            return BucketVerdict(
                symbol=symbol,
                side=side,
                bucket=bucket,
                n=0,
                win_rate=0.0,
                avg_pnl_pct=0.0,
                median_pnl_pct=0.0,
                pf=0.0,
                top_win_share=0.0,
                passed=False,
                reason="no events",
                extra={"n_with_imbalance": self.n_imb},
            )
        nets = [float(t["net_pnl_pct"]) for t in self.trades]
        wins_count = sum(1 for n in nets if n > 0)
        wr = wins_count / self.n
        avg = sum(nets) / self.n
        med = sorted(nets)[self.n // 2] if self.n % 2 == 1 else (
            sorted(nets)[self.n // 2 - 1] + sorted(nets)[self.n // 2]
        ) / 2
        gross_profit = sum(self.wins)
        gross_loss = sum(self.losses)
        if gross_loss > 0:
            pf = gross_profit / gross_loss
        elif gross_profit > 0:
            pf = float("inf")
        else:
            pf = 0.0
        if gross_profit > 0 and self.wins:
            top_win = max(self.wins)
            top_share = top_win / gross_profit
        else:
            top_share = 0.0
        passed, reason = _evaluate_promotion(n=self.n, pf=pf, med=med, top_win_share=top_share)
        return BucketVerdict(
            symbol=symbol,
            side=side,
            bucket=bucket,
            n=self.n,
            win_rate=round(wr, 4),
            avg_pnl_pct=round(avg, 4),
            median_pnl_pct=round(med, 4),
            pf=round(pf, 4) if pf != float("inf") else float("inf"),
            top_win_share=round(top_share, 4),
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            passed=passed,
            reason=reason,
            extra={"n_with_imbalance": self.n_imb},
        )


def _evaluate_promotion(*, n: int, pf: float, med: float, top_win_share: float) -> tuple[bool, str]:
    """Apply the standard promotion gate. Returns (passed, reason)."""
    if n < PROMOTION_N:
        return False, f"n={n} < {PROMOTION_N} (sample too small)"
    if pf == float("inf"):
        return True, "all gates met (inf PF — gross_loss=0; treat as edge only if n>=30)"
    if pf <= PROMOTION_PF:
        return False, f"PF={pf:.2f} <= {PROMOTION_PF}"
    if med <= 0:
        return False, f"median_pnl_pct={med:+.4f} <= 0"
    if top_win_share > PROMOTION_TOP_WIN_SHARE:
        return False, f"top_win_share={top_win_share:.2%} > {PROMOTION_TOP_WIN_SHARE:.0%}"
    return True, "all gates met"


# --------------------------- per-event processing ------------------------- #


def _process_cascade(
    cascade: dict,
    candles: list[dict],
    *,
    symbol: str,
    side: str,
    horizon: int,
    wait: int,
    ask_heavy_threshold: float,
    bid_heavy_threshold: float,
    agg_generic_failed_reclaim: _BucketAgg,
    agg_ask_heavy_any: _BucketAgg,
    agg_bid_heavy_any: _BucketAgg,
    agg_ask_heavy_and_failed_reclaim: _BucketAgg,
    agg_bid_heavy_and_failed_reclaim: _BucketAgg,
    agg_ask_heavy_and_always_fade: _BucketAgg,
    agg_ask_heavy_and_always_follow: _BucketAgg,
) -> None:
    """Process one cascade and update per-bucket aggregations.

    A cascade contributes to:
      - agg_generic_failed_reclaim: any cascade, no filter, only if
        no reclaim (control bucket).
      - agg_ask_heavy_*: only if cascade had top_book_imbalance < ask_heavy_threshold
      - agg_bid_heavy_*: only if cascade had top_book_imbalance > bid_heavy_threshold
      - "always_fade" means enter FADE on the entry bar (no wait).
      - "always_follow" means enter CONTINUATION on the entry bar (no wait).
      - "failed_reclaim_continuation" means wait then enter CONTINUATION
        only if no reclaim.
    """
    event_vwap = float(cascade.get("event_vwap", 0) or 0)
    imbalance = cascade.get("top_book_imbalance")
    imbalance_val: float | None
    if imbalance is None:
        imbalance_val = None
    else:
        try:
            imbalance_val = float(imbalance)
        except (TypeError, ValueError):
            imbalance_val = None

    bbo_bucket = _bbo_bucket(imbalance_val)

    entry_idx = find_entry_idx(candles, cascade.get("start_ts", ""), DEFAULT_MAX_ENTRY_LAG_MINUTES)
    if entry_idx is None:
        return
    ex = _exit_px(candles, entry_idx, horizon)
    if ex is None:
        return
    exit_px, exit_idx = ex
    entry_price = _entry_px(candles, entry_idx)
    if entry_price is None:
        return

    # --- Bucket 1: generic failed_reclaim_continuation (control) --- #
    reclaimed = _reclaim_detected(side, event_vwap, candles, entry_idx, wait)
    if not reclaimed:
        # Enter CONTINUATION at end of wait window (entry_idx + wait).
        cont_entry_idx = min(entry_idx + wait, len(candles) - 1)
        cont_entry = _entry_px(candles, cont_entry_idx)
        cont_ex = _exit_px(candles, cont_entry_idx, horizon - wait) if cont_entry_idx + (horizon - wait) < len(candles) else None
        if cont_entry is not None and cont_ex is not None:
            cont_exit_px, cont_exit_idx = cont_ex
            cont_direction = _continuation_direction(side)
            rec = _trade_record(
                cascade=cascade,
                symbol=symbol,
                side=side,
                direction=cont_direction,
                entry_idx=cont_entry_idx,
                exit_idx=cont_exit_idx,
                entry_px=cont_entry,
                exit_px=cont_exit_px,
                bucket="generic_failed_reclaim_continuation",
                reclaimed=reclaimed,
                bbo_bucket=bbo_bucket,
            )
            agg_generic_failed_reclaim.add(rec)
            # Track imbalance coverage on the control bucket too.
            if imbalance_val is not None:
                agg_generic_failed_reclaim.n_imb += 1

    # --- From here on, we need imbalance data. --- #
    if imbalance_val is None:
        return

    # --- Bucket 2: ask_heavy + always_fade (immediate, no wait) --- #
    if imbalance_val < ask_heavy_threshold:
        fade_direction = _fade_direction(side)
        rec = _trade_record(
            cascade=cascade,
            symbol=symbol,
            side=side,
            direction=fade_direction,
            entry_idx=entry_idx,
            exit_idx=exit_idx,
            entry_px=entry_price,
            exit_px=exit_px,
            bucket="ask_heavy_AND_always_fade",
            reclaimed=reclaimed,
            bbo_bucket=bbo_bucket,
        )
        agg_ask_heavy_and_always_fade.add(rec)
        agg_ask_heavy_any.n_imb += 1
        agg_ask_heavy_any.add(rec)  # we'll re-purpose for any-direction join

    # --- Bucket 3: ask_heavy + always_follow (immediate, no wait) --- #
    if imbalance_val < ask_heavy_threshold:
        cont_direction = _continuation_direction(side)
        rec = _trade_record(
            cascade=cascade,
            symbol=symbol,
            side=side,
            direction=cont_direction,
            entry_idx=entry_idx,
            exit_idx=exit_idx,
            entry_px=entry_price,
            exit_px=exit_px,
            bucket="ask_heavy_AND_always_follow",
            reclaimed=reclaimed,
            bbo_bucket=bbo_bucket,
        )
        agg_ask_heavy_and_always_follow.add(rec)

    # --- Bucket 4: ask_heavy + failed_reclaim_continuation (the JOIN) --- #
    if imbalance_val < ask_heavy_threshold and not reclaimed:
        cont_entry_idx = min(entry_idx + wait, len(candles) - 1)
        cont_entry = _entry_px(candles, cont_entry_idx)
        cont_ex = _exit_px(candles, cont_entry_idx, horizon - wait) if cont_entry_idx + (horizon - wait) < len(candles) else None
        if cont_entry is not None and cont_ex is not None:
            cont_exit_px, cont_exit_idx = cont_ex
            cont_direction = _continuation_direction(side)
            rec = _trade_record(
                cascade=cascade,
                symbol=symbol,
                side=side,
                direction=cont_direction,
                entry_idx=cont_entry_idx,
                exit_idx=cont_exit_idx,
                entry_px=cont_entry,
                exit_px=cont_exit_px,
                bucket="ask_heavy_AND_failed_reclaim_continuation",
                reclaimed=reclaimed,
                bbo_bucket=bbo_bucket,
            )
            agg_ask_heavy_and_failed_reclaim.add(rec)

    # --- Bucket 5: bid_heavy + failed_reclaim_continuation (sister test) --- #
    if imbalance_val > bid_heavy_threshold and not reclaimed:
        cont_entry_idx = min(entry_idx + wait, len(candles) - 1)
        cont_entry = _entry_px(candles, cont_entry_idx)
        cont_ex = _exit_px(candles, cont_entry_idx, horizon - wait) if cont_entry_idx + (horizon - wait) < len(candles) else None
        if cont_entry is not None and cont_ex is not None:
            cont_exit_px, cont_exit_idx = cont_ex
            cont_direction = _continuation_direction(side)
            rec = _trade_record(
                cascade=cascade,
                symbol=symbol,
                side=side,
                direction=cont_direction,
                entry_idx=cont_entry_idx,
                exit_idx=cont_exit_idx,
                entry_px=cont_entry,
                exit_px=cont_exit_px,
                bucket="bid_heavy_AND_failed_reclaim_continuation",
                reclaimed=reclaimed,
                bbo_bucket=bbo_bucket,
            )
            agg_bid_heavy_and_failed_reclaim.add(rec)
        # Also record the bid_heavy_any aggregate for completeness
        cont_rec_simple = _trade_record(
            cascade=cascade,
            symbol=symbol,
            side=side,
            direction=cont_direction,
            entry_idx=entry_idx,
            exit_idx=exit_idx,
            entry_px=entry_price,
            exit_px=exit_px,
            bucket="bid_heavy_ANY_follow",
            reclaimed=reclaimed,
            bbo_bucket=bbo_bucket,
        )
        agg_bid_heavy_any.add(cont_rec_simple)


# --------------------------- main loop ----------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT)
    parser.add_argument("--ask-heavy-threshold", type=float, default=ASK_HEAVY_THRESHOLD,
                        help="BBO imbalance < this is ask_heavy (default 0.45)")
    parser.add_argument("--bid-heavy-threshold", type=float, default=BID_HEAVY_THRESHOLD,
                        help="BBO imbalance > this is bid_heavy (default 0.55)")
    parser.add_argument("--symbols", nargs="*", default=list(SYMBOLS),
                        help="Symbols to test (default BTC ETH)")
    args = parser.parse_args()

    print("HyphyLiquid - BTC ask-heavy x failed-reclaim JOIN backtest")
    print("=" * 78)
    print(f"  horizon: {args.horizon} min")
    print(f"  wait: {args.wait} min")
    print(f"  ask_heavy_threshold: top_book_imbalance < {args.ask_heavy_threshold}")
    print(f"  bid_heavy_threshold: top_book_imbalance > {args.bid_heavy_threshold}")
    print(f"  symbols: {args.symbols}")

    cascades = _load_cascades(CASCADES_PATH)
    print(f"\nLoaded {len(cascades)} cascades from {CASCADES_PATH.name}")

    # Per (symbol, side) aggregations.
    aggs: dict[tuple[str, str], dict[str, _BucketAgg]] = {}
    coverage: dict[str, dict[str, int]] = {}
    for sym in args.symbols:
        for side in ("A", "B"):
            coverage[f"{sym}|{side}"] = {
                "total": 0,
                "with_imbalance": 0,
                "ask_heavy": 0,
                "bid_heavy": 0,
                "no_reclaim": 0,
                "ask_heavy_AND_no_reclaim": 0,
            }
            aggs[(sym, side)] = {
                "generic_failed_reclaim_continuation": _BucketAgg(),
                "ask_heavy_ANY": _BucketAgg(),
                "bid_heavy_ANY": _BucketAgg(),
                "ask_heavy_AND_always_fade": _BucketAgg(),
                "ask_heavy_AND_always_follow": _BucketAgg(),
                "ask_heavy_AND_failed_reclaim_continuation": _BucketAgg(),
                "bid_heavy_AND_failed_reclaim_continuation": _BucketAgg(),
            }

    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in args.symbols:
        candles_by_symbol[sym] = _load_candles(sym)
        print(f"  {sym}: {len(candles_by_symbol[sym])} 1m candles loaded")

    # Per-cascade processing.
    for cascade in cascades:
        sym = str(cascade.get("symbol", "")).upper()
        if sym not in args.symbols:
            continue
        side = str(cascade.get("side", ""))
        if side not in ("A", "B"):
            continue
        candles = candles_by_symbol.get(sym, [])
        if not candles:
            continue

        cov = coverage[f"{sym}|{side}"]
        cov["total"] += 1

        imbalance = cascade.get("top_book_imbalance")
        imbalance_val: float | None
        if imbalance is None:
            imbalance_val = None
        else:
            try:
                imbalance_val = float(imbalance)
            except (TypeError, ValueError):
                imbalance_val = None

        if imbalance_val is not None:
            cov["with_imbalance"] += 1
            if imbalance_val < args.ask_heavy_threshold:
                cov["ask_heavy"] += 1
            if imbalance_val > args.bid_heavy_threshold:
                cov["bid_heavy"] += 1

        # Check reclaim once for coverage tracking (don't double-count).
        event_vwap = float(cascade.get("event_vwap", 0) or 0)
        if event_vwap <= 0:
            continue
        entry_idx = find_entry_idx(candles, cascade.get("start_ts", ""), DEFAULT_MAX_ENTRY_LAG_MINUTES)
        if entry_idx is None:
            continue
        ex = _exit_px(candles, entry_idx, args.horizon)
        if ex is None:
            continue
        # Reclaim check
        reclaimed = _reclaim_detected(side, event_vwap, candles, entry_idx, args.wait)
        if not reclaimed:
            cov["no_reclaim"] += 1
            if imbalance_val is not None and imbalance_val < args.ask_heavy_threshold:
                cov["ask_heavy_AND_no_reclaim"] += 1

        # Now run the full per-cascade processor.
        sym_aggs = aggs[(sym, side)]
        _process_cascade(
            cascade,
            candles,
            symbol=sym,
            side=side,
            horizon=args.horizon,
            wait=args.wait,
            ask_heavy_threshold=args.ask_heavy_threshold,
            bid_heavy_threshold=args.bid_heavy_threshold,
            agg_generic_failed_reclaim=sym_aggs["generic_failed_reclaim_continuation"],
            agg_ask_heavy_any=sym_aggs["ask_heavy_ANY"],
            agg_bid_heavy_any=sym_aggs["bid_heavy_ANY"],
            agg_ask_heavy_and_failed_reclaim=sym_aggs["ask_heavy_AND_failed_reclaim_continuation"],
            agg_bid_heavy_and_failed_reclaim=sym_aggs["bid_heavy_AND_failed_reclaim_continuation"],
            agg_ask_heavy_and_always_fade=sym_aggs["ask_heavy_AND_always_fade"],
            agg_ask_heavy_and_always_follow=sym_aggs["ask_heavy_AND_always_follow"],
        )

    # Build verdicts.
    verdicts: list[BucketVerdict] = []
    for (sym, side), sym_aggs in aggs.items():
        for bucket_name, agg in sym_aggs.items():
            verdicts.append(agg.summary(sym, side, bucket_name))

    # Print coverage + verdicts.
    print()
    print("=" * 78)
    print("COVERAGE")
    print("=" * 78)
    print(
        f"  {'sym|side':<10} {'total':>6} {'with_imb':>10} {'ask_heavy':>10} "
        f"{'bid_heavy':>10} {'no_reclaim':>12} {'ask_hvy & no_recl':>18}"
    )
    print("  " + "-" * 80)
    for key in sorted(coverage):
        cov = coverage[key]
        print(
            f"  {key:<10} {cov['total']:>6} {cov['with_imbalance']:>10} "
            f"{cov['ask_heavy']:>10} {cov['bid_heavy']:>10} "
            f"{cov['no_reclaim']:>12} {cov['ask_heavy_AND_no_reclaim']:>18}"
        )

    print()
    print("=" * 78)
    print("BUCKET VERDICTS")
    print("=" * 78)
    print(
        f"  {'sym':<5} {'side':<5} {'bucket':<48} {'n':>4} {'WR%':>6} "
        f"{'avg%':>9} {'med%':>9} {'PF':>7} {'top%':>6}  pass"
    )
    print("  " + "-" * 110)
    # Sort: BTC first, then ETH; A before B; control first then joins.
    for v in sorted(verdicts, key=lambda r: (r.symbol, r.side, r.bucket)):
        avg = v.avg_pnl_pct
        med = v.median_pnl_pct
        pf = v.pf
        pf_str = f"{pf:>6.2f}" if pf != float("inf") else "  inf"
        top_pct = f"{v.top_win_share * 100:>5.1f}%"
        passed = "PASS" if v.passed else "fail"
        print(
            f"  {v.symbol:<5} {v.side:<5} {v.bucket:<48} {v.n:>4} "
            f"{v.win_rate * 100:>5.1f}% {avg:>+8.4f} {med:>+8.4f} "
            f"{pf_str:>7} {top_pct:>6}  {passed}  {v.reason}"
        )

    # Save results.
    out = {
        "horizon": args.horizon,
        "wait": args.wait,
        "ask_heavy_threshold": args.ask_heavy_threshold,
        "bid_heavy_threshold": args.bid_heavy_threshold,
        "symbols": list(args.symbols),
        "constants": {
            "ASK_HEAVY_THRESHOLD": ASK_HEAVY_THRESHOLD,
            "BID_HEAVY_THRESHOLD": BID_HEAVY_THRESHOLD,
            "ROUND_TRIP_COST_BPS": ROUND_TRIP_COST_BPS,
            "STOP_SLIPPAGE_BPS": STOP_SLIPPAGE_BPS,
            "PROMOTION_N": PROMOTION_N,
            "PROMOTION_PF": PROMOTION_PF,
            "PROMOTION_TOP_WIN_SHARE": PROMOTION_TOP_WIN_SHARE,
        },
        "coverage": coverage,
        "verdicts": [v.to_dict() for v in verdicts],
    }
    RESULTS_JSON_PATH.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nWrote {RESULTS_JSON_PATH.name}")

    # Markdown summary.
    _write_markdown_summary(out, SUMMARY_MD_PATH)
    print(f"Wrote {SUMMARY_MD_PATH.name}")
    return 0


def _write_markdown_summary(payload: dict, path: Path) -> None:
    lines: list[str] = []
    lines.append("# BTC ask-heavy x failed-reclaim JOIN backtest")
    lines.append("")
    lines.append(f"- horizon: **{payload['horizon']} min**")
    lines.append(f"- wait: **{payload['wait']} min**")
    lines.append(
        f"- ask_heavy_threshold: top_book_imbalance < **{payload['ask_heavy_threshold']}**"
    )
    lines.append(
        f"- bid_heavy_threshold: top_book_imbalance > **{payload['bid_heavy_threshold']}**"
    )
    lines.append(f"- symbols: {', '.join(payload['symbols'])}")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append("| sym|side | total | with_imb | ask_heavy | bid_heavy | no_reclaim | ask_hvy & no_reclaim |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for key in sorted(payload["coverage"]):
        cov = payload["coverage"][key]
        lines.append(
            f"| {key} | {cov['total']} | {cov['with_imbalance']} | {cov['ask_heavy']} | "
            f"{cov['bid_heavy']} | {cov['no_reclaim']} | {cov['ask_heavy_AND_no_reclaim']} |"
        )
    lines.append("")
    lines.append("## Verdicts")
    lines.append("")
    lines.append("| symbol | side | bucket | n | WR% | avg% | med% | PF | top% | passed | reason |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for v in sorted(payload["verdicts"], key=lambda r: (r["symbol"], r["side"], r["bucket"])):
        pf = v["pf"]
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"| {v['symbol']} | {v['side']} | {v['bucket']} | {v['n']} | "
            f"{v['win_rate'] * 100:.1f}% | {v['avg_pnl_pct']:+.4f} | {v['median_pnl_pct']:+.4f} | "
            f"{pf_str} | {v['top_win_share'] * 100:.1f}% | "
            f"{'PASS' if v['passed'] else 'fail'} | {v['reason']} |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- This is a JOIN backtest (research only). It does NOT touch execution, order_manager, "
        "risk.py, or any live/paper routing."
    )
    lines.append(
        "- The headline test is `ask_heavy_AND_failed_reclaim_continuation` vs. "
        "`generic_failed_reclaim_continuation` (control). If the JOIN passes the gate "
        f"(n>={PROMOTION_N}, PF>{PROMOTION_PF}, med>0, top<={int(PROMOTION_TOP_WIN_SHARE * 100)}%) "
        "AND the control fails, the BBO condition is doing real work, not just filtering noise."
    )
    lines.append(
        "- Sister buckets (`ask_heavy_AND_always_fade`, `ask_heavy_AND_always_follow`, "
        "`bid_heavy_AND_failed_reclaim_continuation`) show whether the value comes from the BBO "
        "filter alone, the failed-reclaim rule alone, or only the JOIN."
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

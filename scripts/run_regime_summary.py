"""Regime summary - post-rebuild evidence collector.

Per docs/2026-08-03-HANDOFF-regime-map.md, after each mature rebuild Marvis
collects regime evidence and appends to a timestamped research file. Pure
collector: no interpretation, no execution advice, no strategy tuning.

Output: data/regime_log/regime_summary_YYYYMMDD.jsonl
        (one line per rebuild, append-only - never overwrites old rows)

Reads:
  - data/cascades.jsonl
  - data/ws_candle/{symbol}_*.jsonl (1m candles for regime/response)
  - data/lane_backtest_btc_eth_fade_or_follow_trades.jsonl
  - data/lane_backtest_alt_range_liq_scalp_trades.jsonl
  - data/lane_backtest_btc_eth_fade_or_follow_btc_side_b_trades.jsonl
  - data/lane_backtest_alt_range_liq_scalp_hype_side_b_trades.jsonl
  - data/trailing_sweep_btc_eth_btc_side_b.json
  - data/.rebuild_baseline.json
  - git HEAD commit hash

Handoff checklist (from regime-map handoff doc):
  1. Rebuild metadata: commit hash, UTC timestamp, cascade counts by symbol and side
  2. Per-symbol regime counts: candle regime, liquidation response, route action
  3. BTC watch pocket: count BTC side=B continuation events, trailing result,
     activation rate, initial-stop rate, median return, PF
  4. ETH rejected lane: confirm no tested ETH bucket crossed the promotion gate
  5. HYPE research pocket: split B-side results by range_normal/range_wide/range_compressed
  6. Safety gate check: verify SOL/HYPE/DOGE/BNB remain execution_allowed=false
  7. Append results to a timestamped research file; do not overwrite old rows
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make `src` importable when run from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_fade_or_follow_backtest import _load_candles  # noqa: E402
from src.strategy.regime import (  # noqa: E402
    V1_TRADE_SYMBOLS,
    RESEARCH_SYMBOLS,
    band_width_bucket,
    classify_candle_regime,
    classify_liquidation_response,
    route_signal,
)

CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
BASELINE_PATH = REPO_ROOT / "data" / ".rebuild_baseline.json"
LANE_BTC_ETH_TRADES = REPO_ROOT / "data" / "lane_backtest_btc_eth_fade_or_follow_trades.jsonl"
LANE_ALT_TRADES = REPO_ROOT / "data" / "lane_backtest_alt_range_liq_scalp_trades.jsonl"
LANE_BTC_B_TRADES = REPO_ROOT / "data" / "lane_backtest_btc_eth_fade_or_follow_btc_side_b_trades.jsonl"
LANE_HYPE_B_TRADES = REPO_ROOT / "data" / "lane_backtest_alt_range_liq_scalp_hype_side_b_trades.jsonl"
TRAILING_BTC_B_JSON = REPO_ROOT / "data" / "trailing_sweep_btc_eth_btc_side_b.json"
REGIME_LOG_DIR = REPO_ROOT / "data" / "regime_log"
ALL_SYMBOLS = sorted(V1_TRADE_SYMBOLS | RESEARCH_SYMBOLS)

# Promotion gate per the regime-map handoff doc (Promotion Guard section).
PROMOTION_N_THRESHOLD = 100
PROMOTION_PF_THRESHOLD = 1.5
PROMOTION_MEDIAN_THRESHOLD_PCT = 0.0


def _git_head_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
        )
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _event_ts_ms(cascade: dict) -> int | None:
    """Return the cascade event timestamp in Unix ms, or None if missing."""
    raw = cascade.get("event_ts_ms")
    if isinstance(raw, (int, float)):
        return int(raw)
    iso = cascade.get("event_ts")
    if isinstance(iso, str):
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return None
    return None


def _find_candle_idx_for_event(candles: list[dict], event_ts_ms: int) -> int | None:
    """Return the smallest idx where candle.t >= event_ts_ms. None if past end."""
    if not candles:
        return None
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if int(candles[mid].get("t", 0)) < event_ts_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(candles) else None


def _classify_cascade(cascade: dict, candles: list[dict]) -> dict:
    """Classify a single cascade into regime + response + route."""
    event_ms = _event_ts_ms(cascade)
    sym = str(cascade.get("symbol", "")).upper()
    side = str(cascade.get("side", ""))
    event_vwap = cascade.get("event_vwap")
    if event_ms is None or side not in {"A", "B"} or not event_vwap:
        return {
            "regime": "no_data",
            "regime_trend": "unknown",
            "regime_band_bucket": "unknown",
            "response": "unknown",
            "route_action": "reject",
            "route_lane": "unknown",
            "execution_allowed": False,
        }

    idx = _find_candle_idx_for_event(candles, event_ms)
    if idx is None or idx <= 0:
        regime = classify_candle_regime(candles, max(idx or 0, 0))
    else:
        regime = classify_candle_regime(candles, idx)

    # Response: closes of the next 3 completed candles after the event
    closes_after: list[float] = []
    if idx is not None:
        for k in range(idx, min(idx + 5, len(candles))):
            close = candles[k].get("c")
            if isinstance(close, (int, float)):
                closes_after.append(float(close))
    response = classify_liquidation_response(side, float(event_vwap), closes_after, wait_minutes=3)
    route = route_signal(sym, side, regime, response)

    return {
        "regime": regime.label,
        "regime_trend": regime.trend,
        "regime_band_bucket": regime.band_width_bucket,
        "response": response.label,
        "reclaim_detected": response.reclaim_detected,
        "route_action": route.action,
        "route_lane": route.lane,
        "execution_allowed": route.execution_allowed,
    }


def _step1_rebuild_metadata(cascades: list[dict], baseline: dict) -> dict:
    counts_by_sym: dict[str, int] = {}
    counts_by_side: dict[str, int] = {}
    counts_by_sym_side: dict[str, int] = {}
    for c in cascades:
        sym = str(c.get("symbol", "")).upper()
        side = str(c.get("side", ""))
        counts_by_sym[sym] = counts_by_sym.get(sym, 0) + 1
        counts_by_side[side] = counts_by_side.get(side, 0) + 1
        if sym and side:
            key = f"{sym}_{side}"
            counts_by_sym_side[key] = counts_by_sym_side.get(key, 0) + 1
    return {
        "commit": _git_head_short(),
        "rebuild_utc": baseline.get("last_rebuild_ts"),
        "last_liquidation_utc": baseline.get("last_liquidation_ts"),
        "total_cascades": len(cascades),
        "cascades_by_symbol": counts_by_sym,
        "cascades_by_side": counts_by_side,
        "cascades_by_symbol_side": counts_by_sym_side,
    }


def _step2_regime_counts(classifications: list[dict], cascades: list[dict]) -> dict:
    """Per-symbol regime/response/route counts."""
    by_sym: dict[str, dict] = {}
    for c, klass in zip(cascades, classifications):
        sym = str(c.get("symbol", "")).upper()
        side = str(c.get("side", ""))
        slot = by_sym.setdefault(
            sym,
            {
                "total": 0,
                "by_regime": {},
                "by_response": {},
                "by_route_action": {},
                "by_side_regime": {},
            },
        )
        slot["total"] += 1
        regime = klass["regime"]
        response = klass["response"]
        action = klass["route_action"]
        slot["by_regime"][regime] = slot["by_regime"].get(regime, 0) + 1
        slot["by_response"][response] = slot["by_response"].get(response, 0) + 1
        slot["by_route_action"][action] = slot["by_route_action"].get(action, 0) + 1
        side_regime = f"{side}|{regime}"
        slot["by_side_regime"][side_regime] = slot["by_side_regime"].get(side_regime, 0) + 1
    return by_sym


def _step3_btc_watch_pocket() -> dict:
    """BTC side=B continuation stats from trailing sweep (uses default path)."""
    return _step3_btc_watch_pocket_from(json_path=TRAILING_BTC_B_JSON)


def _step3_btc_watch_pocket_from(json_path: Path) -> dict:
    """BTC side=B continuation stats from trailing sweep JSON (custom path)."""
    if not json_path.exists():
        return {"status": "missing", "json": str(json_path.name)}
    rows = _load_json(json_path)
    if not isinstance(rows, list):
        return {"status": "malformed", "json": str(json_path.name)}
    if not rows:
        return {"status": "no_eligible_rows", "json": str(json_path.name)}

    candidates: list[dict] = []
    for row in rows:
        if str(row.get("symbol", "")).upper() != "BTC":
            continue
        if str(row.get("stop_model", "")) not in ("fixed_bps", "event_vwap"):
            continue
        try:
            n = int(row.get("n", 0))
            pf_raw = row.get("profit_factor")
            pf = float("inf") if pf_raw == "inf" else float(pf_raw)
            med = float(row.get("median_net_return_pct", 0.0))
            avg = float(row.get("avg_net_return_pct", 0.0))
            act = float(row.get("activation_rate", 0.0))
            init_sl = float(row.get("initial_stop_rate", 0.0))
        except (TypeError, ValueError):
            continue
        candidates.append(
            {
                "variant": row.get("variant"),
                "horizon": row.get("horizon"),
                "stop_model": row.get("stop_model"),
                "config_initial_stop_bps": row.get("config_initial_stop_bps"),
                "vwap_buffer_bps": row.get("vwap_buffer_bps"),
                "activation_r": row.get("activation_r"),
                "trail_bps": row.get("trail_bps"),
                "n": n,
                "pf": pf,
                "avg_net_return_pct": avg,
                "median_net_return_pct": med,
                "activation_rate": act,
                "initial_stop_rate": init_sl,
            }
        )

    if not candidates:
        return {"status": "no_eligible_rows"}

    # Rank: highest PF first, then n desc, then med desc
    candidates.sort(key=lambda c: (-(c["pf"] if c["pf"] != float("inf") else 1e9), -c["n"], -c["median_net_return_pct"]))
    best = candidates[0]

    return {
        "status": "ok",
        "candidate_count": len(candidates),
        "best": best,
        "top3": candidates[:3],
        "promotion_gate": {
            "n_threshold": PROMOTION_N_THRESHOLD,
            "pf_threshold": PROMOTION_PF_THRESHOLD,
            "median_threshold_pct": PROMOTION_MEDIAN_THRESHOLD_PCT,
            "n_met": best["n"] >= PROMOTION_N_THRESHOLD,
            "pf_met": best["pf"] > PROMOTION_PF_THRESHOLD,
            "median_met": best["median_net_return_pct"] > PROMOTION_MEDIAN_THRESHOLD_PCT,
        },
    }


def _step4_eth_rejected_lane() -> dict:
    """Confirm no tested ETH bucket crossed the promotion gate (default path)."""
    return _step4_eth_rejected_lane_from(path=LANE_BTC_ETH_TRADES)


def _step4_eth_rejected_lane_from(path: Path) -> dict:
    """Confirm no tested ETH bucket crossed the promotion gate (custom path)."""
    trades = _load_jsonl(path)
    eth_trades = [t for t in trades if str(t.get("symbol", "")).upper() == "ETH"]
    buckets: dict[tuple[str, str], list[float]] = {}
    for t in eth_trades:
        variant = str(t.get("variant", "unknown"))
        side = str(t.get("side", "unknown"))
        ret = t.get("return_pct")
        if not isinstance(ret, (int, float)):
            continue
        buckets.setdefault((variant, side), []).append(float(ret))

    bucket_summary: list[dict] = []
    any_crossed = False
    for (variant, side), rets in buckets.items():
        n = len(rets)
        if n == 0:
            continue
        rets_sorted = sorted(rets)
        median = rets_sorted[n // 2] if n % 2 == 1 else (rets_sorted[n // 2 - 1] + rets_sorted[n // 2]) / 2
        wins = sum(1 for r in rets if r > 0)
        wr = wins / n
        gross_win = sum(r for r in rets if r > 0)
        gross_loss = -sum(r for r in rets if r < 0)
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        crossed = (
            n >= PROMOTION_N_THRESHOLD
            and pf > PROMOTION_PF_THRESHOLD
            and median > PROMOTION_MEDIAN_THRESHOLD_PCT
        )
        if crossed:
            any_crossed = True
        bucket_summary.append(
            {
                "variant": variant,
                "side": side,
                "n": n,
                "win_rate": round(wr, 4),
                "median_return_pct": round(median, 4),
                "profit_factor": pf,
                "promotion_crossed": crossed,
            }
        )

    return {
        "trade_count": len(eth_trades),
        "any_bucket_crossed": any_crossed,
        "buckets": sorted(bucket_summary, key=lambda b: (-b["n"], b["variant"], b["side"])),
    }


def _step5_hype_research_pocket() -> dict:
    """HYPE B-side lane backtest trades split by band_width bucket (default paths)."""
    return _step5_hype_research_pocket_from(focused_path=LANE_HYPE_B_TRADES, alt_path=LANE_ALT_TRADES)


def _step5_hype_research_pocket_from(focused_path: Path, alt_path: Path) -> dict:
    """HYPE B-side lane backtest trades split by band_width bucket (custom paths)."""
    trades = _load_jsonl(focused_path)
    if not trades:
        # Fall back to the broader alt lane backtest for HYPE B-side coverage
        alt = _load_jsonl(alt_path)
        trades = [t for t in alt if str(t.get("symbol", "")).upper() == "HYPE" and str(t.get("side", "")) == "B"]

    bucket_groups: dict[str, list[dict]] = {}
    for t in trades:
        bw_pct = t.get("band_width_pct")
        if isinstance(bw_pct, (int, float)):
            bw = band_width_bucket(float(bw_pct))
        else:
            bw = str(t.get("band_width") or t.get("regime_band_bucket") or "unknown")
        bucket_groups.setdefault(bw, []).append(t)

    out: dict[str, Any] = {"trade_count": len(trades), "buckets": {}}
    for bw, group in bucket_groups.items():
        rets = [
            float(t[k])
            for t in group
            for k in ("net_return_pct", "return_pct")
            if isinstance(t.get(k), (int, float))
        ]
        if not rets:
            continue
        n = len(rets)
        rets_sorted = sorted(rets)
        median = rets_sorted[n // 2] if n % 2 == 1 else (rets_sorted[n // 2 - 1] + rets_sorted[n // 2]) / 2
        wins = sum(1 for r in rets if r > 0)
        wr = wins / n
        gross_win = sum(r for r in rets if r > 0)
        gross_loss = -sum(r for r in rets if r < 0)
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        out["buckets"][bw] = {
            "n": n,
            "win_rate": round(wr, 4),
            "median_return_pct": round(median, 4),
            "profit_factor": pf,
        }
    return out


def _step6_safety_gate(classifications: list[dict], cascades: list[dict]) -> dict:
    """Verify SOL/HYPE/DOGE/BNB never route to execution_allowed=True."""
    violations: list[dict] = []
    for c, klass in zip(cascades, classifications):
        sym = str(c.get("symbol", "")).upper()
        if sym in V1_TRADE_SYMBOLS:
            continue
        if klass.get("execution_allowed") is True:
            violations.append(
                {
                    "symbol": sym,
                    "side": c.get("side"),
                    "regime": klass.get("regime"),
                    "response": klass.get("response"),
                    "route_action": klass.get("route_action"),
                    "route_lane": klass.get("route_lane"),
                }
            )
    return {
        "non_v1_symbols_checked": sorted(RESEARCH_SYMBOLS),
        "violation_count": len(violations),
        "violations": violations[:20],  # cap to keep log size bounded
        "gate_holds": len(violations) == 0,
    }


def _aggregate_health(classifications: list[dict], cascades: list[dict]) -> dict:
    """Quick coverage diagnostic: how many cascades got a real regime label?"""
    n_total = len(cascades)
    n_real_regime = sum(1 for k in classifications if k["regime"] != "no_data")
    n_real_response = sum(1 for k in classifications if k["response"] != "unknown")
    return {
        "cascades_total": n_total,
        "regime_classified": n_real_regime,
        "regime_missing": n_total - n_real_regime,
        "response_classified": n_real_response,
        "response_missing": n_total - n_real_response,
    }


def _build_log_line() -> dict:
    cascades = _load_jsonl(CASCADES_PATH)
    baseline = _load_baseline()

    # Preload candles per symbol once for regime/response classification
    candles_by_sym: dict[str, list[dict]] = {}
    for sym in ALL_SYMBOLS:
        try:
            candles_by_sym[sym] = _load_candles(sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: failed to load candles for {sym}: {exc}", file=sys.stderr)
            candles_by_sym[sym] = []

    classifications: list[dict] = []
    for c in cascades:
        sym = str(c.get("symbol", "")).upper()
        candles = candles_by_sym.get(sym, [])
        classifications.append(_classify_cascade(c, candles))

    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "rebuild_metadata": _step1_rebuild_metadata(cascades, baseline),
        "regime_counts_by_symbol": _step2_regime_counts(classifications, cascades),
        "btc_watch_pocket": _step3_btc_watch_pocket(),
        "eth_rejected_lane": _step4_eth_rejected_lane(),
        "hype_research_pocket": _step5_hype_research_pocket(),
        "safety_gate": _step6_safety_gate(classifications, cascades),
        "classification_health": _aggregate_health(classifications, cascades),
    }


def _write_log(payload: dict, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = log_dir / f"regime_summary_{day}.jsonl"
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, default=str) + "\n")
    return out


def _print_summary(payload: dict) -> None:
    md = payload["rebuild_metadata"]
    health = payload["classification_health"]
    btc_pocket = payload["btc_watch_pocket"]
    eth = payload["eth_rejected_lane"]
    hype = payload["hype_research_pocket"]
    safety = payload["safety_gate"]

    print("=" * 70)
    print(f"Regime summary @ {payload['ts_utc']}")
    print(f"  commit:             {md['commit']}")
    print(f"  rebuild_utc:        {md['rebuild_utc']}")
    print(f"  cascades:           {md['total_cascades']}  by_symbol: {md['cascades_by_symbol']}")
    print(f"  by_side:            {md['cascades_by_side']}")
    print(f"  classification:     regime {health['regime_classified']}/{health['cascades_total']}  "
          f"response {health['response_classified']}/{health['cascades_total']}")

    print("\nBTC watch pocket (trailing sweep, side=B):")
    if btc_pocket.get("status") == "ok":
        best = btc_pocket["best"]
        gate = btc_pocket["promotion_gate"]
        print(
            f"  best: variant={best['variant']} horizon={best['horizon']} "
            f"stop={best['stop_model']} cfg={best.get('config_initial_stop_bps') or best.get('vwap_buffer_bps') or best.get('atr_mult')} "
            f"actR={best['activation_r']} trail={best['trail_bps']}"
        )
        print(
            f"  stats: n={best['n']} PF={best['pf']} avg={best['avg_net_return_pct']:+.4f}% "
            f"med={best['median_net_return_pct']:+.4f}%  act={best['activation_rate']:.4f} "
            f"initSL={best['initial_stop_rate']:.4f}"
        )
        print(
            f"  gate: n_met={gate['n_met']} pf_met={gate['pf_met']} median_met={gate['median_met']}"
        )
    else:
        print(f"  status: {btc_pocket.get('status')}")

    print("\nETH rejected lane (no promotion gate cross):")
    print(f"  any_bucket_crossed: {eth['any_bucket_crossed']}  (trade_count={eth['trade_count']})")
    for b in eth["buckets"][:6]:
        pf_str = "inf" if b["profit_factor"] == float("inf") else f"{b['profit_factor']:.2f}"
        print(
            f"  {b['variant']:>32} side={b['side']}  n={b['n']:>4}  "
            f"WR={b['win_rate']:.4f}  med={b['median_return_pct']:+.4f}%  PF={pf_str}  "
            f"crossed={b['promotion_crossed']}"
        )

    print("\nHYPE research pocket (lane backtest, side=B):")
    if hype["buckets"]:
        for bw, b in hype["buckets"].items():
            pf_str = "inf" if b["profit_factor"] == float("inf") else f"{b['profit_factor']:.2f}"
            print(f"  band_width={bw:<12} n={b['n']:>3}  WR={b['win_rate']:.4f}  med={b['median_return_pct']:+.4f}%  PF={pf_str}")
    else:
        print("  (no buckets)")

    print("\nSafety gate (non-v1 execution_allowed):")
    print(f"  non_v1_symbols: {safety['non_v1_symbols_checked']}")
    print(f"  violations:     {safety['violation_count']}  gate_holds={safety['gate_holds']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        default=str(REGIME_LOG_DIR),
        help="directory for regime_summary_YYYYMMDD.jsonl (default: data/regime_log)",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="print summary but do not append to log",
    )
    args = parser.parse_args()

    payload = _build_log_line()
    _print_summary(payload)

    if args.print_only:
        return 0

    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = (REPO_ROOT / log_dir).resolve()
    out = _write_log(payload, log_dir)
    print(f"\nWrote summary to {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

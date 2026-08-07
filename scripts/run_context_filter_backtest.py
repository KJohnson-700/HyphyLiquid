"""Context-filter backtest for liquidation cascades (research only).

Tests three new Tier-1 features that use data HyphyLiquid already collects:

  1. Funding Z-score at cascade time.
  2. OI delta x price delta regime.
  3. Time since previous same-symbol cascade.

The script evaluates whether those context buckets improve fade/follow
direction selection. It does not touch execution, risk.py, order manager, or
paper/live routing.
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

from src.strategy.fade_or_follow_backtest import (  # noqa: E402
    _bar_dt,
    _continuation_direction,
    _fade_direction,
    _return_pct,
)

SYMBOLS: tuple[str, ...] = ("BTC", "ETH")
DEFAULT_HORIZONS: tuple[int, ...] = (5, 15, 30, 60)
ROUND_TRIP_COST_BPS: float = 8.0
FUNDING_Z_LOOKBACK: int = 240
FUNDING_Z_MIN_HISTORY: int = 30
OI_LOOKBACK_MINUTES: int = 30
PRICE_DELTA_THRESHOLD_PCT: float = 0.05
OI_DELTA_THRESHOLD_PCT: float = 0.05
PROMOTION_N: int = 30
PROMOTION_PF: float = 1.5
PROMOTION_TOP_WIN_SHARE: float = 0.35

CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
ASSET_CTX_DIR = REPO_ROOT / "data" / "asset_ctx"
CANDLES_DIR = REPO_ROOT / "data" / "ws_candle"
RESULTS_JSON_PATH = REPO_ROOT / "data" / "context_filter_results.json"
SUMMARY_MD_PATH = REPO_ROOT / "data" / "context_filter_summary.md"


@dataclass
class ContextFeatures:
    symbol: str
    event_ts_ms: int
    funding: float | None
    funding_z: float | None
    funding_z_bucket: str
    oi_delta_pct: float | None
    price_delta_pct: float | None
    oi_price_regime: str
    cooldown_minutes: float | None
    cooldown_bucket: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BucketVerdict:
    symbol: str
    side: str
    horizon_minutes: int
    playbook: str
    bucket: str
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


def _file_stem(symbol: str) -> str:
    if ":" not in symbol:
        return symbol.lower()
    dex, market = symbol.split(":", 1)
    return f"{dex.lower()}_{market.lower()}"


def _parse_ts_ms(ts: str) -> int | None:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_cascades(path: Path = CASCADES_PATH) -> list[dict]:
    return _load_jsonl(path)


def _parse_ctx(row: dict) -> dict | None:
    ctx = row.get("context") if isinstance(row, dict) else None
    if not isinstance(ctx, dict):
        return None
    ts = _parse_ts_ms(str(row.get("poll_ts", "")))
    if ts is None:
        return None
    funding = _float_or_none(ctx.get("funding"))
    oi = _float_or_none(ctx.get("openInterest"))
    mark = _float_or_none(ctx.get("markPx") or ctx.get("midPx") or ctx.get("oraclePx"))
    if funding is None and oi is None and mark is None:
        return None
    return {"ts": ts, "funding": funding, "oi": oi, "mark": mark}


def _load_asset_ctx(symbol: str, *, ctx_dir: Path = ASSET_CTX_DIR) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(ctx_dir.glob(f"{_file_stem(symbol)}_*.jsonl")):
        for row in _load_jsonl(path):
            parsed = _parse_ctx(row)
            if parsed is not None:
                rows.append(parsed)
    rows.sort(key=lambda r: r["ts"])
    return rows


def _load_candles(symbol: str, *, candle_dir: Path = CANDLES_DIR) -> list[dict]:
    by_open: dict[int, dict] = {}
    for path in sorted(candle_dir.glob(f"{_file_stem(symbol)}_*.jsonl")):
        for row in _load_jsonl(path):
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            try:
                open_ts = int(payload.get("t", payload.get("ts")))
            except (TypeError, ValueError):
                continue
            by_open[open_ts] = payload
    return [by_open[k] for k in sorted(by_open)]


def _row_at_or_before(rows: list[dict], ts_ms: int) -> int | None:
    if not rows or rows[0]["ts"] > ts_ms:
        return None
    lo, hi = 0, len(rows) - 1
    if rows[hi]["ts"] <= ts_ms:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if rows[mid]["ts"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _funding_z_score(rows: list[dict], idx: int, *, lookback: int = FUNDING_Z_LOOKBACK, min_history: int = FUNDING_Z_MIN_HISTORY) -> float | None:
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


def funding_z_bucket(z: float | None) -> str:
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


def _pct_delta(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def oi_price_regime(price_delta_pct: float | None, oi_delta_pct: float | None, *, price_threshold_pct: float = PRICE_DELTA_THRESHOLD_PCT, oi_threshold_pct: float = OI_DELTA_THRESHOLD_PCT) -> str:
    if price_delta_pct is None or oi_delta_pct is None:
        return "oi_price_unknown"
    price_dir = "flat"
    oi_dir = "flat"
    if price_delta_pct >= price_threshold_pct:
        price_dir = "price_up"
    elif price_delta_pct <= -price_threshold_pct:
        price_dir = "price_down"
    if oi_delta_pct >= oi_threshold_pct:
        oi_dir = "oi_up"
    elif oi_delta_pct <= -oi_threshold_pct:
        oi_dir = "oi_down"
    if price_dir == "flat" and oi_dir == "flat":
        return "price_flat_oi_flat"
    return f"{price_dir}_{oi_dir}"


def cooldown_bucket(minutes: float | None) -> str:
    if minutes is None:
        return "first_cascade"
    if minutes < 5:
        return "cooldown_hot_lt5m"
    if minutes < 15:
        return "cooldown_warm_5_15m"
    if minutes < 60:
        return "cooldown_room_15_60m"
    return "cooldown_fresh_60m_plus"


def compute_context_features(cascade: dict, ctx_rows: list[dict], prior_ts_ms: int | None) -> ContextFeatures:
    ts_ms = int(cascade.get("event_ts_ms") or _parse_ts_ms(str(cascade.get("start_ts", ""))) or 0)
    idx = _row_at_or_before(ctx_rows, ts_ms) if ts_ms else None
    funding = funding_z = oi_delta = price_delta = None
    regime = "oi_price_unknown"
    if idx is not None:
        row = ctx_rows[idx]
        funding = row.get("funding")
        funding_z = _funding_z_score(ctx_rows, idx)
        lookback_ts = ts_ms - OI_LOOKBACK_MINUTES * 60_000
        prior_idx = _row_at_or_before(ctx_rows, lookback_ts)
        if prior_idx is not None:
            price_delta = _pct_delta(row.get("mark"), ctx_rows[prior_idx].get("mark"))
            oi_delta = _pct_delta(row.get("oi"), ctx_rows[prior_idx].get("oi"))
            regime = oi_price_regime(price_delta, oi_delta)
    cooldown = ((ts_ms - prior_ts_ms) / 60_000.0) if ts_ms and prior_ts_ms is not None else None
    return ContextFeatures(
        symbol=str(cascade.get("symbol", "")),
        event_ts_ms=ts_ms,
        funding=funding,
        funding_z=funding_z,
        funding_z_bucket=funding_z_bucket(funding_z),
        oi_delta_pct=oi_delta,
        price_delta_pct=price_delta,
        oi_price_regime=regime,
        cooldown_minutes=cooldown,
        cooldown_bucket=cooldown_bucket(cooldown),
    )


def _close(bar: dict) -> float | None:
    try:
        return float(bar.get("c") or bar.get("payload", {}).get("c"))
    except (TypeError, ValueError):
        return None


def _bar_ts_ms(bar: dict) -> int | None:
    try:
        return int(bar.get("t") or bar.get("payload", {}).get("t"))
    except (TypeError, ValueError):
        return None


def _entry_idx(candles: list[dict], candle_ts: list[int], cascade_start_ts: str, max_entry_lag_minutes: int = 2) -> int | None:
    event_ts = _parse_ts_ms(cascade_start_ts)
    if event_ts is None or not candle_ts:
        return None
    lo, hi = 0, len(candle_ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if candle_ts[mid] <= event_ts:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(candles):
        return None
    lag_s = (candle_ts[lo] - event_ts) / 1000.0
    if lag_s > max_entry_lag_minutes * 60:
        return None
    return lo


def _trade_return(cascade: dict, candles: list[dict], *, horizon: int, playbook: str, max_entry_lag_minutes: int = 2) -> float | None:
    candle_ts = [_bar_ts_ms(c) or 0 for c in candles]
    idx = _entry_idx(candles, candle_ts, str(cascade.get("start_ts", "")), max_entry_lag_minutes)
    if idx is None:
        return None
    exit_idx = idx + horizon
    if exit_idx >= len(candles):
        return None
    entry = _close(candles[idx])
    exit_px = _close(candles[exit_idx])
    if entry is None or exit_px is None or entry <= 0 or exit_px <= 0:
        return None
    side = str(cascade.get("side", ""))
    direction = _fade_direction(side) if playbook == "fade" else _continuation_direction(side)
    return _return_pct(direction, entry, exit_px) - ROUND_TRIP_COST_BPS / 100.0


def _profit_factor(values: list[float]) -> tuple[float, float, float]:
    gross_profit = sum(v for v in values if v > 0)
    gross_loss = abs(sum(v for v in values if v < 0))
    if gross_loss == 0:
        pf = float("inf") if gross_profit > 0 else 0.0
    else:
        pf = gross_profit / gross_loss
    return pf, gross_profit, gross_loss


def _top_win_share(values: list[float], gross_profit: float) -> float:
    wins = [v for v in values if v > 0]
    if not wins or gross_profit <= 0:
        return 0.0
    return max(wins) / gross_profit


def apply_promotion_gate(verdict: BucketVerdict) -> BucketVerdict:
    reasons: list[str] = []
    if verdict.n < PROMOTION_N:
        reasons.append(f"n<{PROMOTION_N}")
    if verdict.pf <= PROMOTION_PF:
        reasons.append(f"pf<={PROMOTION_PF}")
    if verdict.median_pnl_pct <= 0:
        reasons.append("median<=0")
    if verdict.top_win_share > PROMOTION_TOP_WIN_SHARE:
        reasons.append(f"top_win_share>{PROMOTION_TOP_WIN_SHARE}")
    verdict.passed = not reasons
    verdict.reason = "pass" if verdict.passed else ", ".join(reasons)
    return verdict


def summarize_bucket(symbol: str, side: str, horizon: int, playbook: str, bucket: str, values: list[float]) -> BucketVerdict:
    values = list(values)
    pf, gross_profit, gross_loss = _profit_factor(values)
    wins = [v for v in values if v > 0]
    verdict = BucketVerdict(
        symbol=symbol,
        side=side,
        horizon_minutes=horizon,
        playbook=playbook,
        bucket=bucket,
        n=len(values),
        win_rate=round(len(wins) / len(values) * 100.0, 2) if values else 0.0,
        avg_pnl_pct=round(sum(values) / len(values), 4) if values else 0.0,
        median_pnl_pct=round(statistics.median(values), 4) if values else 0.0,
        pf=round(pf, 4) if pf != float("inf") else float("inf"),
        top_win_share=round(_top_win_share(values, gross_profit), 4),
        gross_profit=round(gross_profit, 4),
        gross_loss=round(gross_loss, 4),
    )
    return apply_promotion_gate(verdict)


def run_backtest(symbols: tuple[str, ...], horizons: tuple[int, ...]) -> dict:
    cascades = [
        c for c in _load_cascades()
        if str(c.get("symbol", "")).upper() in symbols and c.get("side") in {"A", "B"}
    ]
    ctx_by_symbol = {sym: _load_asset_ctx(sym) for sym in symbols}
    candles_by_symbol = {sym: _load_candles(sym) for sym in symbols}
    candle_ts_by_symbol = {sym: [_bar_ts_ms(c) or 0 for c in candles_by_symbol[sym]] for sym in symbols}
    prior_by_symbol: dict[str, int] = {}
    features_by_key: dict[str, ContextFeatures] = {}
    buckets: dict[tuple[str, str, int, str, str], list[float]] = defaultdict(list)
    feature_rows: list[dict] = []

    for cascade in sorted(cascades, key=lambda c: int(c.get("event_ts_ms") or 0)):
        sym = str(cascade.get("symbol", "")).upper()
        ts_ms = int(cascade.get("event_ts_ms") or 0)
        features = compute_context_features(cascade, ctx_by_symbol.get(sym, []), prior_by_symbol.get(sym))
        prior_by_symbol[sym] = ts_ms or prior_by_symbol.get(sym, 0)
        key = f"{sym}|{cascade.get('side')}|{cascade.get('start_ts')}|{round(float(cascade.get('total_notional', 0) or 0), 2)}"
        features_by_key[key] = features
        feature_rows.append({"cascade_key": key, **features.to_dict()})
        feature_names = {
            f"funding_z:{features.funding_z_bucket}",
            f"oi_price:{features.oi_price_regime}",
            f"cooldown:{features.cooldown_bucket}",
            f"combo:{features.funding_z_bucket}|{features.oi_price_regime}|{features.cooldown_bucket}",
        }
        for horizon in horizons:
            for playbook in ("fade", "follow"):
                ret = _trade_return_fast(
                    cascade,
                    candles_by_symbol.get(sym, []),
                    candle_ts_by_symbol.get(sym, []),
                    horizon=horizon,
                    playbook=playbook,
                )
                if ret is None:
                    continue
                for feature_name in feature_names:
                    buckets[(sym, str(cascade.get("side")), horizon, playbook, feature_name)].append(ret)

    verdicts = [
        summarize_bucket(sym, side, horizon, playbook, bucket, values)
        for (sym, side, horizon, playbook, bucket), values in buckets.items()
    ]
    verdicts.sort(key=lambda v: (not v.passed, -v.n, -v.pf if v.pf != float("inf") else -9999, v.symbol, v.bucket))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(symbols),
        "horizons": list(horizons),
        "feature_rows": feature_rows,
        "verdicts": [v.to_dict() for v in verdicts],
        "passed": [v.to_dict() for v in verdicts if v.passed],
    }


def _trade_return_fast(cascade: dict, candles: list[dict], candle_ts: list[int], *, horizon: int, playbook: str, max_entry_lag_minutes: int = 2) -> float | None:
    idx = _entry_idx(candles, candle_ts, str(cascade.get("start_ts", "")), max_entry_lag_minutes)
    if idx is None:
        return None
    exit_idx = idx + horizon
    if exit_idx >= len(candles):
        return None
    entry = _close(candles[idx])
    exit_px = _close(candles[exit_idx])
    if entry is None or exit_px is None or entry <= 0 or exit_px <= 0:
        return None
    side = str(cascade.get("side", ""))
    direction = _fade_direction(side) if playbook == "fade" else _continuation_direction(side)
    return _return_pct(direction, entry, exit_px) - ROUND_TRIP_COST_BPS / 100.0


def render_markdown(result: dict, *, limit: int = 40) -> str:
    lines = [
        "# Context Filter Backtest",
        "",
        f"Generated: `{result['generated_at']}`",
        f"Symbols: `{', '.join(result['symbols'])}`",
        f"Horizons: `{', '.join(str(h) for h in result['horizons'])}`",
        "",
        "## Passed Buckets",
        "",
    ]
    passed = result.get("passed", [])
    if not passed:
        lines.append("- none")
    else:
        for row in passed[:limit]:
            lines.append(
                f"- {row['symbol']} side={row['side']} {row['playbook']} {row['horizon_minutes']}m "
                f"{row['bucket']}: n={row['n']}, PF={row['pf']}, WR={row['win_rate']}%, "
                f"avg/med={row['avg_pnl_pct']}%/{row['median_pnl_pct']}%, top={row['top_win_share']}"
            )
    lines.extend(["", "## Top Buckets", ""])
    for row in result.get("verdicts", [])[:limit]:
        lines.append(
            f"- {row['symbol']} side={row['side']} {row['playbook']} {row['horizon_minutes']}m "
            f"{row['bucket']}: n={row['n']}, PF={row['pf']}, WR={row['win_rate']}%, "
            f"avg/med={row['avg_pnl_pct']}%/{row['median_pnl_pct']}%, gate={row['reason']}"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    args = parser.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    horizons = tuple(int(h.strip()) for h in args.horizons.split(",") if h.strip())
    result = run_backtest(symbols, horizons)
    RESULTS_JSON_PATH.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    SUMMARY_MD_PATH.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "results": str(RESULTS_JSON_PATH),
        "summary": str(SUMMARY_MD_PATH),
        "feature_rows": len(result["feature_rows"]),
        "verdicts": len(result["verdicts"]),
        "passed": len(result["passed"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Feature-bucket diagnostics for BTC/ETH cascade backtests.

This module joins simulated lane trades back to their enriched cascade rows
and summarizes return quality by deterministic filter buckets. It is analysis
only; it does not create signals or touch execution.
"""
from __future__ import annotations

from statistics import mean, median
from typing import Any, Iterable


def trade_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return the stable key shared by lane trades and cascades."""
    return (
        str(row.get("cascade_start_ts") or row.get("start_ts") or ""),
        str(row.get("symbol") or "").upper(),
        str(row.get("side") or ""),
    )


def _float_value(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def spread_bucket(spread_bps: Any) -> str:
    """Bucket BBO spread in raw-price basis points."""
    value = _float_value(spread_bps)
    if value is None:
        return "missing"
    if value <= 0.25:
        return "tight"
    if value <= 1.0:
        return "normal"
    return "wide"


def imbalance_bucket(imbalance: Any) -> str:
    """Bucket top-of-book imbalance from -1 ask-heavy to +1 bid-heavy."""
    value = _float_value(imbalance)
    if value is None:
        return "missing"
    if value <= -0.25:
        return "ask_heavy"
    if value >= 0.25:
        return "bid_heavy"
    return "balanced"


def funding_bucket(funding: Any) -> str:
    """Bucket hourly funding rate without assuming directionality."""
    value = _float_value(funding)
    if value is None:
        return "missing"
    if value <= -0.000005:
        return "negative"
    if value >= 0.000005:
        return "positive"
    return "flat"


def notional_bucket(total_notional: Any) -> str:
    """Bucket liquidation cluster size in USD notional."""
    value = _float_value(total_notional)
    if value is None:
        return "missing"
    if value < 500_000:
        return "lt_500k"
    if value < 1_000_000:
        return "500k_1m"
    if value < 3_000_000:
        return "1m_3m"
    return "gte_3m"


def fill_count_bucket(n_fills: Any) -> str:
    """Bucket liquidation fill count."""
    value = _float_value(n_fills)
    if value is None:
        return "missing"
    if value < 25:
        return "lt_25"
    if value < 100:
        return "25_99"
    return "gte_100"


def staleness_bucket(delta_s: Any) -> str:
    """Bucket snapshot staleness by absolute seconds from event time."""
    value = _float_value(delta_s)
    if value is None:
        return "missing"
    value = abs(value)
    if value <= 5:
        return "fresh"
    if value <= 30:
        return "usable"
    if value <= 120:
        return "stale"
    return "too_stale"


def oi_level_buckets(cascades: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Return per-symbol low/mid/high OI labels keyed by cascade key."""
    values_by_symbol: dict[str, list[float]] = {}
    rows = list(cascades)
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        oi = _float_value(row.get("oi"))
        if symbol and oi is not None:
            values_by_symbol.setdefault(symbol, []).append(oi)

    thresholds: dict[str, tuple[float, float]] = {}
    for symbol, values in values_by_symbol.items():
        ordered = sorted(values)
        if not ordered:
            continue
        low_idx = max(0, min(len(ordered) - 1, len(ordered) // 3))
        high_idx = max(0, min(len(ordered) - 1, (len(ordered) * 2) // 3))
        thresholds[symbol] = (ordered[low_idx], ordered[high_idx])

    out: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (str(row.get("start_ts") or ""), str(row.get("symbol") or "").upper())
        symbol = key[1]
        oi = _float_value(row.get("oi"))
        if oi is None or symbol not in thresholds:
            out[key] = "missing"
            continue
        low, high = thresholds[symbol]
        if oi <= low:
            out[key] = "low"
        elif oi >= high:
            out[key] = "high"
        else:
            out[key] = "mid"
    return out


def _summarize(rows: list[dict[str, Any]], return_field: str) -> dict[str, Any]:
    returns = [_float_value(row.get(return_field)) for row in rows]
    clean_returns = [r for r in returns if r is not None]
    if not clean_returns:
        return {
            "n": 0,
            "win_rate": 0.0,
            "avg_return_pct": 0.0,
            "median_return_pct": 0.0,
            "profit_factor": 0.0,
            "top_win_share": 0.0,
        }
    wins = [r for r in clean_returns if r > 0]
    losses = [r for r in clean_returns if r <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    top_win = max(wins) if wins else 0.0
    return {
        "n": len(clean_returns),
        "win_rate": round(len(wins) / len(clean_returns), 4),
        "avg_return_pct": round(mean(clean_returns), 4),
        "median_return_pct": round(median(clean_returns), 4),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "top_win_share": round(top_win / gross_profit, 4) if gross_profit > 0 else 0.0,
    }


def enrich_trades_with_filters(
    trades: Iterable[dict[str, Any]],
    cascades: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach deterministic cascade feature buckets to each serialized trade."""
    cascade_rows = list(cascades)
    by_key = {trade_key(row): row for row in cascade_rows}
    oi_buckets = oi_level_buckets(cascade_rows)
    out: list[dict[str, Any]] = []

    for trade in trades:
        key = trade_key(trade)
        cascade = by_key.get(key)
        if cascade is None:
            continue
        start_ts, symbol, _side = key
        enriched = dict(trade)
        enriched.update(
            {
                "bbo_spread_bucket": spread_bucket(cascade.get("bbo_spread_bps")),
                "top_book_imbalance_bucket": imbalance_bucket(cascade.get("top_book_imbalance")),
                "funding_bucket": funding_bucket(cascade.get("funding")),
                "predicted_funding_bucket": funding_bucket(cascade.get("predicted_funding")),
                "notional_bucket": notional_bucket(cascade.get("total_notional")),
                "fill_count_bucket": fill_count_bucket(cascade.get("n_fills")),
                "l2_staleness_bucket": staleness_bucket(cascade.get("l2_delta_s")),
                "ctx_staleness_bucket": staleness_bucket(cascade.get("ctx_delta_s")),
                "oi_level_bucket": oi_buckets.get((start_ts, symbol), "missing"),
                "bbo_spread_bps": _float_value(cascade.get("bbo_spread_bps")),
                "top_book_imbalance": _float_value(cascade.get("top_book_imbalance")),
                "funding": _float_value(cascade.get("funding")),
                "predicted_funding": _float_value(cascade.get("predicted_funding")),
                "total_notional": _float_value(cascade.get("total_notional")),
                "n_fills": _float_value(cascade.get("n_fills")),
            }
        )
        out.append(enriched)
    return out


def diagnostic_groups(
    trades: Iterable[dict[str, Any]],
    *,
    return_field: str = "return_pct",
    min_n: int = 20,
) -> list[dict[str, Any]]:
    """Summarize feature buckets and return rows sorted by evidence quality."""
    rows = list(trades)
    group_fields = (
        ("variant",),
        ("symbol",),
        ("side",),
        ("symbol", "side"),
        ("variant", "symbol", "side"),
        ("bbo_spread_bucket",),
        ("top_book_imbalance_bucket",),
        ("funding_bucket",),
        ("predicted_funding_bucket",),
        ("notional_bucket",),
        ("fill_count_bucket",),
        ("l2_staleness_bucket",),
        ("ctx_staleness_bucket",),
        ("oi_level_bucket",),
        ("variant", "symbol", "side", "bbo_spread_bucket"),
        ("variant", "symbol", "side", "top_book_imbalance_bucket"),
        ("variant", "symbol", "side", "funding_bucket"),
        ("variant", "symbol", "side", "oi_level_bucket"),
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        for fields in group_fields:
            key = "|".join(f"{field}={row.get(field, 'missing')}" for field in fields)
            grouped.setdefault(("+".join(fields), key), []).append(row)

    summaries: list[dict[str, Any]] = []
    for (group, bucket), group_rows in grouped.items():
        summary = _summarize(group_rows, return_field)
        if summary["n"] < min_n:
            continue
        summaries.append({"group": group, "bucket": bucket, **summary})

    def _pf_sort_value(row: dict[str, Any]) -> float:
        raw = row["profit_factor"]
        return 999.0 if raw == "inf" else float(raw)

    return sorted(
        summaries,
        key=lambda r: (
            -_pf_sort_value(r),
            -float(r["median_return_pct"]),
            -int(r["n"]),
            str(r["group"]),
            str(r["bucket"]),
        ),
    )

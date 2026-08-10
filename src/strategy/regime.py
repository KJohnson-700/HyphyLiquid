"""Deterministic regime labels for liquidation strategy routing.

The regime layer is intentionally rule-based. AI assistants can summarize,
review, and propose thresholds around these labels, but v1 execution should
only consume deterministic outputs that can be replayed in tests.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from src.strategy.lane_backtest import atr_at, bollinger_at

V1_TRADE_SYMBOLS = {"BTC", "ETH"}
RESEARCH_SYMBOLS = {"SOL", "HYPE", "DOGE", "BNB", "xyz:GOLD", "xyz:SILVER"}


@dataclass(frozen=True)
class CandleRegime:
    """Regime label derived from prior completed candles."""

    label: str
    trend: str
    band_width_bucket: str
    band_width_pct: float
    atr_pct: float | None
    slope_pct: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LiquidationResponse:
    """Event response label derived from closes after a liquidation burst."""

    label: str
    reclaim_detected: bool
    bars_checked: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RegimeRoute:
    """Asset-specific routing decision for a candidate signal."""

    action: str
    lane: str
    execution_allowed: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def band_width_bucket(width_pct: float) -> str:
    """Map Bollinger band width percent to stable range buckets."""
    if width_pct <= 0.5:
        return "compressed"
    if width_pct <= 1.0:
        return "normal"
    if width_pct <= 2.0:
        return "wide"
    return "very_wide"


def classify_candle_regime(
    candles: list[dict],
    idx: int,
    *,
    band_period: int = 20,
    stdev_mult: float = 2.0,
    trend_lookback: int = 20,
    trend_threshold_pct: float = 0.20,
    atr_period: int = 14,
    high_atr_pct: float = 0.50,
) -> CandleRegime:
    """Classify trend/range state using only completed candles before idx.

    Args:
        candles: One-minute candles in chronological order.
        idx: Candidate entry index. The candle at idx is excluded.
        band_period: Prior closes used for Bollinger band width.
        stdev_mult: Bollinger standard-deviation multiplier.
        trend_lookback: Prior-close lookback for trend slope.
        trend_threshold_pct: Absolute slope threshold for trend labels.
        atr_period: Prior bars used for ATR.
        high_atr_pct: ATR percent threshold that overrides range labels.
    """
    if idx <= 0 or idx > len(candles):
        return CandleRegime("no_data", "unknown", "unknown", 0.0, None, 0.0, "idx outside candle coverage")

    bands = bollinger_at(candles, idx, period=band_period, stdev_mult=stdev_mult)
    if bands is None:
        return CandleRegime("no_data", "unknown", "unknown", 0.0, None, 0.0, "insufficient band history")

    try:
        latest_close = float(candles[idx - 1].get("c") or candles[idx - 1].get("payload", {}).get("c"))
        start_idx = max(0, idx - trend_lookback)
        start_close = float(candles[start_idx].get("c") or candles[start_idx].get("payload", {}).get("c"))
    except (TypeError, ValueError):
        return CandleRegime("no_data", "unknown", "unknown", 0.0, None, 0.0, "bad candle close data")

    slope_pct = ((latest_close - start_close) / start_close * 100.0) if start_close > 0 else 0.0
    if slope_pct >= trend_threshold_pct:
        trend = "trend_up"
    elif slope_pct <= -trend_threshold_pct:
        trend = "trend_down"
    else:
        trend = "range"

    atr = atr_at(candles, idx, period=atr_period)
    atr_pct = (atr / latest_close * 100.0) if atr is not None and latest_close > 0 else None
    bucket = band_width_bucket(float(bands["width_pct"]))

    if atr_pct is not None and atr_pct >= high_atr_pct:
        label = "high_vol_cascade"
        reason = f"atr_pct {atr_pct:.4f} >= {high_atr_pct:.4f}"
    elif trend != "range":
        label = trend
        reason = f"slope_pct {slope_pct:.4f} crossed trend threshold"
    else:
        label = f"range_{bucket}"
        reason = f"band_width_pct {bands['width_pct']:.4f} bucketed as {bucket}"

    return CandleRegime(
        label=label,
        trend=trend,
        band_width_bucket=bucket,
        band_width_pct=round(float(bands["width_pct"]), 4),
        atr_pct=round(atr_pct, 4) if atr_pct is not None else None,
        slope_pct=round(slope_pct, 4),
        reason=reason,
    )


def classify_liquidation_response(
    side: str,
    event_vwap: float,
    closes_after_event: Iterable[float],
    *,
    wait_minutes: int = 3,
) -> LiquidationResponse:
    """Classify post-liquidation reclaim versus failed-reclaim continuation."""
    if side not in {"A", "B"} or event_vwap <= 0:
        return LiquidationResponse("unknown", False, 0, "missing side or event_vwap")

    checked = 0
    for close in closes_after_event:
        if checked >= wait_minutes:
            break
        checked += 1
        if side == "B" and close < event_vwap:
            return LiquidationResponse(
                "post_liquidation_reclaim",
                True,
                checked,
                "B-side cascade closed back below event_vwap",
            )
        if side == "A" and close > event_vwap:
            return LiquidationResponse(
                "post_liquidation_reclaim",
                True,
                checked,
                "A-side cascade closed back above event_vwap",
            )

    if checked == 0:
        return LiquidationResponse("unknown", False, checked, "no post-event closes available")
    return LiquidationResponse(
        "post_liquidation_continuation",
        False,
        checked,
        f"no reclaim inside {checked} checked bar(s)",
    )


def route_signal(
    symbol: str,
    side: str,
    candle_regime: CandleRegime,
    response: LiquidationResponse,
) -> RegimeRoute:
    """Route a signal by asset, side, candle regime, and liquidation response."""
    sym = symbol.upper()
    if sym not in V1_TRADE_SYMBOLS | RESEARCH_SYMBOLS:
        return RegimeRoute("reject", "unknown", False, "symbol outside configured scope")

    if sym == "BTC":
        if (
            side == "B"
            and response.label == "post_liquidation_continuation"
            and candle_regime.label in {"trend_up", "high_vol_cascade", "range_wide", "range_very_wide"}
        ):
            return RegimeRoute(
                "watch",
                "btc_eth_trailing_resolution",
                True,
                "BTC B-side continuation is the current v1 watchlist pocket",
            )
        return RegimeRoute(
            "reject",
            "btc_eth_fade_or_follow",
            True,
            "BTC route only watches B-side failed-reclaim continuation for now",
        )

    if sym == "ETH":
        if side == "A" and response.label == "post_liquidation_continuation":
            return RegimeRoute(
                "watch",
                "eth_funding_context_follow",
                True,
                "ETH A-side continuation with elevated funding is the current v1 paper candidate",
            )
        if side == "B" and response.label == "post_liquidation_continuation":
            return RegimeRoute(
                "research_candidate",
                "eth_book_persistence_fade",
                False,
                "ETH B-side book-persistence lane is retired from new paper opens after negative forward paper",
            )
        if side == "B":
            return RegimeRoute(
                "watch",
                "eth_book_persistence_fade",
                False,
                "ETH B-side book-persistence lane is retired from new paper opens",
            )
        return RegimeRoute(
            "collect_only",
            "eth_funding_context_follow",
            True,
            "ETH A-side did not show continuation; reclaim visible",
        )

    if sym == "HYPE":
        if side == "B" and candle_regime.label in {"range_normal", "range_wide"}:
            return RegimeRoute(
                "research_candidate",
                "alt_range_liq_scalp",
                False,
                "HYPE B-side normal/wide range is the only alt watch pocket",
            )
        if candle_regime.label == "range_compressed":
            return RegimeRoute(
                "reject",
                "alt_range_liq_scalp",
                False,
                "compressed-band HYPE bucket has been negative in current diagnostics",
            )
        return RegimeRoute("watch", "alt_range_liq_scalp", False, "HYPE remains research-only")

    return RegimeRoute(
        "collect_only",
        "alt_range_liq_scalp",
        False,
        f"{sym} has insufficient signal sample; collect data only",
    )

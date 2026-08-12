"""Execution price rounding helpers for Hyperliquid orders."""
from __future__ import annotations


V1_PRICE_TICKS: dict[str, float] = {
    "BTC": 1.0,
    "ETH": 0.1,
}


def round_to_tick(symbol: str, price: float) -> float:
    """Round a price to the configured v1 Hyperliquid tick size."""
    if price <= 0:
        raise ValueError("price must be positive")
    tick = V1_PRICE_TICKS.get(symbol.upper(), 0.01)
    return round(round(price / tick) * tick, 6)


def aggressive_ioc_limit_px(symbol: str, mark_px: float, is_buy: bool, slippage_bps: float) -> float:
    """Return an aggressive IOC limit price rounded to the symbol tick."""
    if mark_px <= 0:
        raise ValueError("mark_px must be positive")
    slip = max(0.0, slippage_bps) / 10_000.0
    raw_px = mark_px * (1.0 + slip) if is_buy else mark_px * (1.0 - slip)
    return round_to_tick(symbol, raw_px)

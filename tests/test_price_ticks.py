"""Price ticks must match the venue.

A tick coarser than the real one is a valid price but quantises away edge on a
one-tick-spread market; a tick finer than the real one gets the order rejected.
Values verified against the Hyperliquid L2 book on 2026-08-24.
"""
from src.execution.pricing import V1_PRICE_TICKS, round_to_tick

VERIFIED = {"BTC": 1.0, "ETH": 0.1, "HYPE": 0.001, "SOL": 0.001}


def test_verified_ticks_present():
    for sym, tick in VERIFIED.items():
        assert V1_PRICE_TICKS.get(sym) == tick, f"{sym} tick drifted"


def test_rounding_lands_on_the_tick():
    assert round_to_tick("HYPE", 78.0114) == 78.011
    assert round_to_tick("SOL", 96.8306) == 96.831
    assert round_to_tick("BTC", 78992.4) == 78992.0


def test_unknown_symbol_falls_back_coarse():
    """The 0.01 default is deliberately conservative for unlisted symbols."""
    assert round_to_tick("DOGE", 0.12345) == 0.12

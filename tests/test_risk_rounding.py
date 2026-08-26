"""Rounding must never push a trade over the limit sizing just satisfied.

Found on the first real testnet signal (HYPE, 2026-08-26 12:00 UTC). Intended
size 3.205784 rounded up to 3.21, the stop rounded to the 0.001 tick, and risk
landed at $10.0120 against a $10.00 cap -- REJECTED_RISK_PCT by 1.2 cents. A
strategy that sizes to exactly max_risk trips this on EVERY trade, so it would
have blocked live trading entirely.
"""
from unittest.mock import MagicMock

from src.execution.order_manager import OrderManager
from src.risk import RiskConfig, RiskManager


def _om(bankroll=1000.0, risk_pct=0.01, sz_decimals=2):
    """HYPE is szDecimals=2 on the venue; a MagicMock info would fall back to 3
    and the reproduction would not reproduce."""
    om = OrderManager(exchange=MagicMock(), info=MagicMock(),
                      bankroll=bankroll, risk_per_trade_pct=risk_pct, env="testnet")
    om._risk = RiskManager(RiskConfig(bankroll_usd=bankroll,
                                      max_risk_per_trade_pct=risk_pct))
    om._size_decimals = lambda symbol: sz_decimals
    return om


def test_the_hype_rejection_now_sizes_within_the_cap():
    om = _om()
    entry, stop_distance = 81.873, 81.873 - 78.754   # post-tick-rounding
    size = 262.4671916010499 / entry                 # 3.205784
    r = om._round_size_capped("HYPE", size, entry, stop_distance)
    assert r * stop_distance <= 1000.0 * 0.01 + 1e-9, "risk still breaches the cap"
    assert r == 3.20, f"expected floor to 3.20, got {r}"


def test_without_stop_distance_behaviour_is_unchanged():
    """The leverage-only call sites must keep working exactly as before."""
    om = _om()
    entry = 81.873
    size = 262.4671916010499 / entry
    assert om._round_size_capped("HYPE", size, entry) == 3.21


def test_leverage_cap_still_enforced():
    om = _om(bankroll=1000.0, sz_decimals=5)
    om.max_leverage = 10.0
    entry = 60000.0
    r = om._round_size_capped("BTC", 10000.0 / entry, entry)
    assert r * entry <= 1000.0 * 10.0 + 1e-9


def test_leverage_clamp_applies_even_to_an_oversized_intent():
    """Leverage is a hard clamp, unlike the risk correction which only fixes
    rounding. An oversized intent still gets clamped to the leverage cap and
    then rejected on risk downstream."""
    om = _om(sz_decimals=5)
    om.max_leverage = 10.0
    entry, stop_distance = 100.0, 0.5
    r = om._round_size_capped("BTC", 500.0, entry, stop_distance)
    assert r * entry <= 10000.0 + 1e-9, "leverage cap must still bind"
    assert r * stop_distance > 10.0, "risk breach must survive to be rejected"


def test_zero_stop_distance_is_ignored_not_divided_by():
    om = _om()
    assert om._round_size_capped("HYPE", 3.205784, 81.873, 0.0) == 3.21


def test_a_genuinely_oversized_intent_is_NOT_silently_shrunk():
    """The correction must fix rounding, never override the caller.

    First version of this fix resized a $9,000 intent down to ~$600 and let it
    through. That hides a caller mistake instead of surfacing it -- an oversized
    request must still reach the risk check and be rejected.
    """
    om = _om(sz_decimals=5)
    entry, stop_distance = 60000.0, 1000.0        # risk cap allows 0.01 coin
    oversized = 9000.0 / entry                    # 0.15 coin = $150 risk vs $10 cap
    r = om._round_size_capped("BTC", oversized, entry, stop_distance)
    assert r * stop_distance > 10.0, "oversized intent must stay oversized so risk rejects it"

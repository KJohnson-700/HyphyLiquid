"""A zero-size rejection must say WHY, not just that it happened.

ZEC signalled six consecutive hours on testnet and every attempt returned
'position size rounds to zero' with no explanation. The cause was a venue
difference: ZEC is szDecimals=0 on testnet (whole coins) and 2 on mainnet, so a
$125 position at $816/coin rounds to nothing. Six identical opaque rejections
cost hours; one explicit message would have cost seconds.
"""
from unittest.mock import MagicMock

from src.execution.order_manager import OrderManager, BracketOrderIntent
from src.risk import RiskConfig, RiskManager
import pandas as pd


def _om(sz_decimals):
    om = OrderManager(exchange=MagicMock(), info=MagicMock(), bankroll=1000.0,
                      risk_per_trade_pct=0.01, env="testnet")
    om._risk = RiskManager(RiskConfig(bankroll_usd=1000.0, max_risk_per_trade_pct=0.01))
    om._size_decimals = lambda s: sz_decimals
    om._round_to_tick = lambda s, p: round(p, 2)
    return om


def _intent():
    return BracketOrderIntent(signal_ts=pd.Timestamp("2026-08-27 19:00", tz="UTC"),
                              symbol="ZEC", side="long", entry_px=816.23,
                              sl_px=750.93, tp_px=914.18, notional_usd=125.0,
                              reason="test")


def test_zero_size_names_the_asset_and_the_venue_minimum():
    r = _om(0).execute_bracket_intent(_intent())
    assert r.status == "rejected_below_min_size"
    for token in ("ZEC", "szDecimals=0", "untradeable"):
        assert token in r.error


def test_it_reports_the_risk_the_minimum_would_carry():
    """The actionable number: one whole coin risks far more than the budget."""
    r = _om(0).execute_bracket_intent(_intent())
    assert "$10.00" in r.error          # the budget
    assert "against" in r.error


def test_a_normal_precision_asset_is_not_rejected_for_size():
    r = _om(2).execute_bracket_intent(_intent())
    assert r.status != "rejected_below_min_size"

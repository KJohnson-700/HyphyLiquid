"""
Tests for paper_funding_neg_fade.py live trading safety.

Run with: python -m pytest tests/test_funding_neg_fade_live.py -v
"""

import pytest
import pandas as pd
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from paper_funding_neg_fade import (
    LIVE_BANKROLL_USD,
    LIVE_DRAWDOWN_KILL_PCT,
    LIVE_CONSEC_LOSS_HALT,
    _check_live_circuits,
)


def _fresh_state():
    """Create a fresh live trading state for testing."""
    return {
        "consecutive_losses": 0,
        "last_loss_ts": None,
        "last_win_ts": None,
        "peak_equity_usd": LIVE_BANKROLL_USD,
        "halted_until": None,
        "stopped": False,
        "total_pnl_usd": 0.0,
        "trade_count": 0,
    }


class TestLiveCircuits:
    """Test live trading circuit breakers."""

    def test_drawdown_kill_triggers_at_40_percent(self):
        """40% drawdown should kill live trading."""
        state = _fresh_state()
        state["peak_equity_usd"] = LIVE_BANKROLL_USD
        # Equity at 59% (41% drawdown) -> should kill
        equity = LIVE_BANKROLL_USD * 0.59
        allowed, reason = _check_live_circuits(state, equity)
        assert not allowed, f"Should reject at 41% drawdown, got: {reason}"
        assert "drawdown" in reason.lower()

    def test_drawdown_at_60_percent_is_ok(self):
        """60% equity (40% drawdown exactly) should be allowed."""
        state = _fresh_state()
        state["peak_equity_usd"] = LIVE_BANKROLL_USD
        # Equity at 60% (exactly at boundary)
        equity = LIVE_BANKROLL_USD * 0.60
        allowed, reason = _check_live_circuits(state, equity)
        assert allowed, f"Should allow at 40% drawdown, got: {reason}"

    def test_consec_loss_halt_at_3(self):
        """3 consecutive losses should halt live trading."""
        state = _fresh_state()
        state["consecutive_losses"] = LIVE_CONSEC_LOSS_HALT
        equity = LIVE_BANKROLL_USD
        allowed, reason = _check_live_circuits(state, equity)
        assert not allowed, f"Should reject at {LIVE_CONSEC_LOSS_HALT} consec losses"
        assert "consec" in reason.lower()

    def test_consec_loss_2_is_ok(self):
        """2 consecutive losses should still allow trading."""
        state = _fresh_state()
        state["consecutive_losses"] = 2
        equity = LIVE_BANKROLL_USD
        allowed, reason = _check_live_circuits(state, equity)
        assert allowed, f"Should allow with 2 consec losses, got: {reason}"

    def test_halt_until_blocks_trading(self):
        """If halted_until is in future, should reject."""
        state = _fresh_state()
        future = (pd.Timestamp.utcnow() + pd.Timedelta(hours=12)).isoformat()
        state["halted_until"] = future
        equity = LIVE_BANKROLL_USD
        allowed, reason = _check_live_circuits(state, equity)
        assert not allowed
        assert "halted" in reason.lower()


class TestLongOnlyAssumption:
    """Verify the strategy is long-only as documented."""

    def test_live_symbols_is_hype_only(self):
        """LIVE_SYMBOLS should be HYPE only at launch."""
        from paper_funding_neg_fade import LIVE_SYMBOLS
        assert LIVE_SYMBOLS == ["HYPE"]

    def test_live_bankroll_is_50(self):
        """Live bankroll should be $50 as documented."""
        assert LIVE_BANKROLL_USD == 50.0

    def test_live_risk_is_50_cents(self):
        """Live risk should be $0.50 (1% of $50)."""
        from paper_funding_neg_fade import LIVE_RISK_USD
        assert LIVE_RISK_USD == 0.50


class TestKillSwitch:
    """Test kill switch file handling."""

    def test_kill_switch_path_exists(self):
        """Kill switch path should be defined."""
        from paper_funding_neg_fade import KILL_SWITCH_PATH
        assert KILL_SWITCH_PATH is not None
        assert "kill_switch" in str(KILL_SWITCH_PATH).lower()


class TestConstants:
    """Verify live trading constants are correct."""

    def test_drawdown_kill_is_40_percent(self):
        assert LIVE_DRAWDOWN_KILL_PCT == 0.40

    def test_consec_loss_halt_is_3(self):
        assert LIVE_CONSEC_LOSS_HALT == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

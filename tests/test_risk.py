"""
Tests for src/risk.py — the safety layer.

These tests are the FIRST thing to run after any risk.py change.
If these fail, do not deploy.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.risk import (
    Position,
    RiskConfig,
    RiskManager,
    RiskState,
    RiskVerdict,
    TradeResult,
)


@pytest.fixture
def cfg() -> RiskConfig:
    return RiskConfig(bankroll_usd=1000.0)


@pytest.fixture
def rm(cfg: RiskConfig) -> RiskManager:
    return RiskManager(cfg, RiskState())


class TestBasicApproval:
    def test_normal_trade_approved(self, rm: RiskManager) -> None:
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=8)
        assert v == RiskVerdict.APPROVED

    def test_zero_size_approved(self, rm: RiskManager) -> None:
        v = rm.check_trade("BTC", "long", size_usd=0, leverage=1, stop_distance_usd=0)
        assert v == RiskVerdict.APPROVED

    def test_short_side_works(self, rm: RiskManager) -> None:
        v = rm.check_trade("ETH", "short", size_usd=5000, leverage=10, stop_distance_usd=8)
        assert v == RiskVerdict.APPROVED


class TestLeverageLimit:
    def test_11x_rejected(self, rm: RiskManager) -> None:
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=11, stop_distance_usd=8)
        assert v == RiskVerdict.REJECTED_LEVERAGE

    def test_10x_approved(self, rm: RiskManager) -> None:
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=8)
        assert v == RiskVerdict.APPROVED

    def test_50x_rejected(self, rm: RiskManager) -> None:
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=50, stop_distance_usd=8)
        assert v == RiskVerdict.REJECTED_LEVERAGE


class TestRiskPerTrade:
    def test_2pct_risk_rejected(self, rm: RiskManager) -> None:
        # $20 risk on $1000 bankroll = 2%
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=20)
        assert v == RiskVerdict.REJECTED_RISK_PCT

    def test_1pct_risk_approved(self, rm: RiskManager) -> None:
        # $10 risk on $1000 bankroll = 1%
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=10)
        assert v == RiskVerdict.APPROVED

    def test_exactly_1pct_approved(self, rm: RiskManager) -> None:
        # $10.01 should be just over the limit
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=10.01)
        assert v == RiskVerdict.REJECTED_RISK_PCT


class TestDailyLossLimit:
    def test_daily_limit_triggers(self, rm: RiskManager) -> None:
        # Lose $30 today (3% of $1000)
        rm.record_trade_close(TradeResult("BTC", "long", -30, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.REJECTED_DAILY_LIMIT

    def test_daily_limit_exact_boundary(self, rm: RiskManager) -> None:
        # $29 loss (under 3% limit) — should still be approved
        rm.record_trade_close(TradeResult("BTC", "long", -29, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.APPROVED


class TestConsecutiveLossHalt:
    def test_three_losses_halts_24h(self, rm: RiskManager) -> None:
        for _ in range(3):
            rm.record_trade_close(TradeResult("BTC", "long", -5, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.REJECTED_CONSECUTIVE_LOSSES
        assert rm.state.halted_until is not None
        assert rm.state.halted_until > datetime.now(timezone.utc)

    def test_two_losses_does_not_halt(self, rm: RiskManager) -> None:
        for _ in range(2):
            rm.record_trade_close(TradeResult("BTC", "long", -5, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.APPROVED

    def test_win_resets_streak(self, rm: RiskManager) -> None:
        for _ in range(2):
            rm.record_trade_close(TradeResult("BTC", "long", -5, datetime.now(timezone.utc)))
        rm.record_trade_close(TradeResult("BTC", "long", +10, datetime.now(timezone.utc)))
        assert rm.state.consecutive_losses == 0
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.APPROVED


class TestDrawdownKill:
    def test_40pct_drawdown_kills(self, rm: RiskManager, cfg: RiskConfig) -> None:
        # Lose $400 (40%) — should trigger kill BEFORE daily limit is checked
        rm.record_trade_close(TradeResult("BTC", "long", -400, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.REJECTED_DRAWDOWN
        assert rm.state.stopped is True

    def test_39pct_drawdown_does_not_kill(self, rm: RiskManager) -> None:
        # Lose $390 (39%) — should hit DAILY limit (>$30) but NOT the kill switch
        rm.record_trade_close(TradeResult("BTC", "long", -390, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        # 39% drawdown trips the daily limit (3% = $30) but not the 40% kill
        assert v == RiskVerdict.REJECTED_DAILY_LIMIT
        assert rm.state.stopped is False

    def test_kill_is_permanent(self, rm: RiskManager) -> None:
        rm.record_trade_close(TradeResult("BTC", "long", -400, datetime.now(timezone.utc)))
        rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        # Even after a winning day, the kill switch stays on
        rm.record_trade_close(TradeResult("BTC", "long", +100, datetime.now(timezone.utc)))
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.REJECTED_DRAWDOWN


class TestMaxPositions:
    def test_three_positions_rejects_fourth(self, rm: RiskManager) -> None:
        for sym in ["BTC", "ETH", "SOL"]:
            rm.open_position(
                Position(sym, "long", 100, 5, 50000, datetime.now(timezone.utc))
            )
        v = rm.check_trade("AVAX", "long", size_usd=500, leverage=5, stop_distance_usd=5)
        assert v == RiskVerdict.REJECTED_MAX_POSITIONS

    def test_close_position_frees_slot(self, rm: RiskManager) -> None:
        for sym in ["BTC", "ETH", "SOL"]:
            rm.open_position(
                Position(sym, "long", 100, 5, 50000, datetime.now(timezone.utc))
            )
        rm.close_position("BTC")
        v = rm.check_trade("AVAX", "long", size_usd=500, leverage=5, stop_distance_usd=5)
        assert v == RiskVerdict.APPROVED


class TestWeeklyLimit:
    def test_weekly_limit_triggers(self, rm: RiskManager) -> None:
        # Build up weekly P&L slowly without triggering consecutive-loss halt
        # OR daily limit. Alternate wins and losses to keep streak <=2.
        # Daily limit: 3% = $30. Weekly limit: 5% = $50.
        now = datetime.now(timezone.utc)
        rm.record_trade_close(TradeResult("BTC", "long", -20, now))                          # day 0
        rm.record_trade_close(TradeResult("BTC", "long", +5, now - timedelta(days=1)))      # day 1
        rm.record_trade_close(TradeResult("BTC", "long", -20, now - timedelta(days=2)))     # day 2
        rm.record_trade_close(TradeResult("BTC", "long", +5, now - timedelta(days=3)))      # day 3
        rm.record_trade_close(TradeResult("BTC", "long", -25, now - timedelta(days=4)))     # day 4
        # Daily: only the day-0 -$20 counts (others on different days) = -$20 (under -$30)
        # Weekly: -20 +5 -20 +5 -25 = -$55 = 5.5% > 5% → weekly limit triggered
        # Streak: was 1 (just -$25), under halt threshold of 3
        v = rm.check_trade("BTC", "long", size_usd=5000, leverage=10, stop_distance_usd=5)
        assert v == RiskVerdict.REJECTED_WEEKLY_LIMIT

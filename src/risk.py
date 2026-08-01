"""
HyphyLiquid — Risk Module

The single source of truth for "should we take this trade?"
Every other module calls into here. No exceptions.

HARD RULES (per AGENTS.md §5):
- Max risk per trade: 1% of bankroll
- Max leverage: 10x
- Max open positions: 3
- Daily loss limit: 3% of bankroll
- Weekly loss limit: 5% of bankroll
- 3 consecutive losses → halt 24h
- 40% drawdown → STOP

If RiskManager.check_trade() returns anything other than APPROVED,
the trade does not happen. Period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Optional


class RiskVerdict(Enum):
    """All possible outcomes of a risk check. APPROVED is the only green light."""

    APPROVED = "approved"
    REJECTED_DAILY_LIMIT = "rejected: daily loss limit hit"
    REJECTED_WEEKLY_LIMIT = "rejected: weekly loss limit hit"
    REJECTED_CONSECUTIVE_LOSSES = "rejected: halted after consecutive losses"
    REJECTED_DRAWDOWN = "rejected: drawdown kill triggered"
    REJECTED_MAX_POSITIONS = "rejected: max open positions reached"
    REJECTED_LEVERAGE = "rejected: leverage exceeds cap"
    REJECTED_RISK_PCT = "rejected: risk per trade exceeds 1%"


@dataclass
class RiskConfig:
    """Static risk parameters. Loaded from config/settings.yaml at startup."""

    bankroll_usd: float = 1000.0
    max_risk_per_trade_pct: float = 0.01
    max_leverage: float = 10.0
    max_open_positions: int = 3
    daily_loss_limit_pct: float = 0.03
    weekly_loss_limit_pct: float = 0.05
    consecutive_loss_halt: int = 3
    drawdown_kill_pct: float = 0.40


@dataclass
class Position:
    """An open position tracked by the risk module."""

    symbol: str
    side: str  # "long" or "short"
    size_usd: float
    leverage: float
    entry_price: float
    entry_time: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class TradeResult:
    """A closed trade, used to update consecutive loss tracking and P&L totals."""

    symbol: str
    side: str
    pnl_usd: float
    closed_at: datetime


@dataclass
class RiskState:
    """Mutable state tracked across calls. Persist between bot restarts."""

    open_positions: List[Position] = field(default_factory=list)
    closed_trades_today: List[TradeResult] = field(default_factory=list)
    closed_trades_week: List[TradeResult] = field(default_factory=list)
    consecutive_losses: int = 0
    halted_until: Optional[datetime] = None
    stopped: bool = False
    bankroll_at_session_start: float = 1000.0

    def daily_pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.closed_trades_today)

    def weekly_pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.closed_trades_week)

    def current_equity(self, cfg: RiskConfig) -> float:
        """Current equity = starting bankroll + realized P&L today."""
        return cfg.bankroll_usd + self.daily_pnl_usd()


class RiskManager:
    """
    Hard-coded risk rules. The bot does not trade unless this says APPROVED.

    Usage:
        rm = RiskManager(cfg, state)
        verdict = rm.check_trade(
            symbol="BTC",
            side="long",
            size_usd=5000,
            leverage=10,
            stop_distance_usd=8,  # how much $ we'd lose if stop hit
        )
        if verdict == RiskVerdict.APPROVED:
            place_order(...)
    """

    def __init__(self, config: RiskConfig, state: Optional[RiskState] = None):
        self.config = config
        self.state = state or RiskState()
        self.state.bankroll_at_session_start = config.bankroll_usd

    def check_trade(
        self,
        symbol: str,
        side: str,
        size_usd: float,
        leverage: float,
        stop_distance_usd: float,
    ) -> RiskVerdict:
        """Return APPROVED or a specific REJECTED reason. Call BEFORE every order.

        Checks are ordered by severity (most severe / most permanent first):
            1. Hard kill switch (stopped, manual or drawdown)
            2. Drawdown kill (40% drawdown → permanent stop)
            3. Halt (consecutive losses → 24h pause)
            4. Daily loss limit
            5. Weekly loss limit
            6. Max open positions
            7. Leverage cap
            8. Risk per trade
        """
        cfg = self.config
        st = self.state
        now = datetime.now(timezone.utc)

        # 1. Hard kill switch (set by drawdown, reset only manually)
        if st.stopped:
            return RiskVerdict.REJECTED_DRAWDOWN

        # 2. Drawdown kill (40% from session start → permanent stop)
        equity = st.current_equity(cfg)
        if equity <= cfg.bankroll_usd * (1 - cfg.drawdown_kill_pct):
            st.stopped = True
            return RiskVerdict.REJECTED_DRAWDOWN

        # 3. Halt check (consecutive losses, etc.) — temporary 24h pause
        if st.halted_until and now < st.halted_until:
            return RiskVerdict.REJECTED_CONSECUTIVE_LOSSES

        # 4. Daily loss limit
        daily_limit = cfg.bankroll_usd * cfg.daily_loss_limit_pct
        if -st.daily_pnl_usd() >= daily_limit:
            return RiskVerdict.REJECTED_DAILY_LIMIT

        # 5. Weekly loss limit
        weekly_limit = cfg.bankroll_usd * cfg.weekly_loss_limit_pct
        if -st.weekly_pnl_usd() >= weekly_limit:
            return RiskVerdict.REJECTED_WEEKLY_LIMIT

        # 6. Max open positions
        if len(st.open_positions) >= cfg.max_open_positions:
            return RiskVerdict.REJECTED_MAX_POSITIONS

        # 7. Leverage cap
        if leverage > cfg.max_leverage:
            return RiskVerdict.REJECTED_LEVERAGE

        # 8. Risk per trade (as % of bankroll)
        if cfg.bankroll_usd > 0:
            risk_pct = stop_distance_usd / cfg.bankroll_usd
            if risk_pct > cfg.max_risk_per_trade_pct:
                return RiskVerdict.REJECTED_RISK_PCT

        return RiskVerdict.APPROVED

    def record_trade_close(self, result: TradeResult) -> None:
        """
        Call after every closed trade. Updates consecutive loss tracking and
        may trigger a halt. Rolls the daily/weekly windows so only trades
        from the right window count toward their respective limits.
        """
        st = self.state
        now = datetime.now(timezone.utc)

        # Append first, then roll both windows to filter the new trade
        # into the correct window (today vs. this week).
        st.closed_trades_today.append(result)
        st.closed_trades_today = [
            t for t in st.closed_trades_today
            if t.closed_at.date() == now.date()
        ]

        cutoff = now - timedelta(days=7)
        st.closed_trades_week.append(result)
        st.closed_trades_week = [
            t for t in st.closed_trades_week if t.closed_at >= cutoff
        ]

        if result.pnl_usd < 0:
            st.consecutive_losses += 1
            if st.consecutive_losses >= self.config.consecutive_loss_halt:
                st.halted_until = now + timedelta(hours=24)
        else:
            st.consecutive_losses = 0

    def open_position(self, position: Position) -> None:
        st = self.state
        st.open_positions.append(position)

    def close_position(self, symbol: str) -> None:
        st = self.state
        st.open_positions = [p for p in st.open_positions if p.symbol != symbol]

    def reset_daily(self) -> None:
        """Call at UTC midnight to reset daily P&L tracking."""
        self.state.closed_trades_today = []

    def reset_weekly(self) -> None:
        """Call at week boundary to reset weekly P&L tracking."""
        self.state.closed_trades_week = []

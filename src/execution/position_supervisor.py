"""Position lifecycle supervisor for live-like execution.

The strategy opens a position; the supervisor owns timed exits and the
reduce-only close intent. This keeps the exit lifecycle separate from signal
generation and makes the live boundary easier to audit.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.execution.order_manager import V1_TRADE_SYMBOLS
from src.execution.paper_broker import PaperBracket, PaperPosition
from src.execution.paper_intents import ACTIVE_EXECUTION_LANE
from src.execution.pricing import aggressive_ioc_limit_px


@dataclass(frozen=True)
class ManagedPosition:
    """Open position shape required by the lifecycle supervisor."""

    position_id: str
    symbol: str
    side: str
    size_coin: float
    entry_ts: datetime
    max_hold_minutes: int
    source: str = ""


@dataclass(frozen=True)
class ReduceOnlyCloseIntent:
    """A deterministic reduce-only close instruction."""

    position_id: str
    symbol: str
    is_buy: bool
    size_coin: float
    reason: str
    reduce_only: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TimeoutDecision:
    """Supervisor decision for a single managed position."""

    action: str
    reason: str
    due_at: datetime | None
    now: datetime
    close_intent: ReduceOnlyCloseIntent | None = None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["due_at"] = self.due_at.isoformat() if self.due_at else None
        out["now"] = self.now.isoformat()
        return out


@dataclass(frozen=True)
class CloseResult:
    """Result from submitting a reduce-only close intent."""

    submitted: bool
    status: str
    response: Any | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_timeout_decision(position: ManagedPosition, *, now: datetime | None = None) -> TimeoutDecision:
    """Return whether a position should be closed due to max hold time."""
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    symbol = position.symbol.upper()
    if symbol not in V1_TRADE_SYMBOLS:
        return TimeoutDecision("reject", f"{symbol} is not in v1 execution allowlist", None, now_utc)
    side = position.side.lower()
    if side not in {"long", "short"}:
        return TimeoutDecision("reject", "position side must be long or short", None, now_utc)
    if position.size_coin <= 0:
        return TimeoutDecision("reject", "position size must be positive", None, now_utc)
    if position.max_hold_minutes <= 0:
        return TimeoutDecision("reject", "max_hold_minutes must be positive", None, now_utc)

    due_at = _as_utc(position.entry_ts) + timedelta(minutes=position.max_hold_minutes)
    if now_utc < due_at:
        return TimeoutDecision("hold", "max hold has not elapsed", due_at, now_utc)

    intent = ReduceOnlyCloseIntent(
        position_id=position.position_id,
        symbol=symbol,
        is_buy=(side == "short"),
        size_coin=position.size_coin,
        reason=f"timeout_exit:{position.max_hold_minutes}m",
    )
    return TimeoutDecision("close", "max hold elapsed; submit reduce-only close", due_at, now_utc, intent)


def execute_reduce_only_close(
    exchange: Any,
    intent: ReduceOnlyCloseIntent,
    *,
    mark_px: float | None = None,
    slippage_bps: float = 20.0,
) -> CloseResult:
    """Submit a reduce-only close intent through an injected exchange client.

    Hyperliquid SDK versions expose slightly different helpers. Prefer
    `market_close` when present; otherwise use an aggressive reduce-only IOC
    limit order, which is the same close primitive the supervisor needs.
    """
    if intent.symbol.upper() not in V1_TRADE_SYMBOLS:
        return CloseResult(False, "rejected_v1_allowlist", error=f"{intent.symbol} is not v1-eligible")
    if not intent.reduce_only:
        return CloseResult(False, "rejected", error="close intent must be reduce-only")
    if intent.size_coin <= 0:
        return CloseResult(False, "rejected", error="close size must be positive")

    try:
        if hasattr(exchange, "market_close"):
            response = _call_market_close(exchange, intent)
        else:
            if mark_px is None or mark_px <= 0:
                return CloseResult(False, "rejected", error="mark_px is required for IOC close fallback")
            limit_px = aggressive_ioc_limit_px(intent.symbol, mark_px, intent.is_buy, slippage_bps)
            response = exchange.order(
                intent.symbol,
                intent.is_buy,
                intent.size_coin,
                limit_px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=True,
            )
        return CloseResult(True, "submitted", response=response)
    except Exception as exc:
        return CloseResult(False, "exchange_error", error=str(exc))


def latest_open_eth_managed_position(data_dir: Path) -> ManagedPosition | None:
    """Return latest still-open ETH funding-follow paper position as supervisor input."""
    rows = _load_position_rows(data_dir)
    closed_ids = {
        str(row.get("paper_id"))
        for row in rows
        if row.get("event") == "mark"
        and isinstance(row.get("fill"), dict)
        and row["fill"].get("status") == "closed"
    }
    latest: PaperPosition | None = None
    for row in rows:
        if row.get("event") != "opened":
            continue
        if str(row.get("paper_id")) in closed_ids:
            continue
        if row.get("symbol") != "ETH" or row.get("lane") != ACTIVE_EXECUTION_LANE:
            continue
        position = _position_from_row(row)
        if position is not None:
            latest = position
    if latest is None:
        return None
    return _managed_from_paper(latest)


def build_latest_eth_timeout_preview(data_dir: Path, *, now: datetime | None = None) -> dict:
    """Build a canary-safe timeout supervisor preview for the latest ETH position."""
    position = latest_open_eth_managed_position(data_dir)
    if position is None:
        return {
            "eligible": False,
            "reason": "no open ETH funding-context paper position needs supervision",
            "lane": ACTIVE_EXECUTION_LANE,
        }
    decision = build_timeout_decision(position, now=now)
    payload = decision.to_dict()
    payload.update(
        {
            "eligible": decision.action in {"hold", "close"},
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side,
            "source": position.source,
        }
    )
    return payload


def _managed_from_paper(position: PaperPosition) -> ManagedPosition:
    size_coin = position.notional_usd / position.entry_price if position.entry_price > 0 else 0.0
    return ManagedPosition(
        position_id=position.paper_id,
        symbol=position.symbol,
        side=position.direction,
        size_coin=round(size_coin, 8),
        entry_ts=_parse_ts(position.entry_ts),
        max_hold_minutes=position.bracket.max_hold_minutes,
        source=f"paper:{position.lane}",
    )


def _call_market_close(exchange: Any, intent: ReduceOnlyCloseIntent) -> Any:
    try:
        return exchange.market_close(intent.symbol, intent.size_coin)
    except TypeError:
        return exchange.market_close(intent.symbol, sz=intent.size_coin)


def _load_position_rows(data_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(data_dir.glob("paper_positions_*.jsonl")):
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _position_from_row(row: dict) -> PaperPosition | None:
    try:
        bracket = PaperBracket(**row["bracket"])
        return PaperPosition(
            paper_id=row["paper_id"],
            paper_scope=row["paper_scope"],
            cascade_key=row["cascade_key"],
            symbol=row["symbol"],
            side=row["side"],
            lane=row["lane"],
            direction=row["direction"],
            event_ts=row["event_ts"],
            entry_ts=row["entry_ts"],
            entry_idx=int(row["entry_idx"]),
            entry_price=float(row["entry_price"]),
            notional_usd=float(row["notional_usd"]),
            risk_usd=float(row["risk_usd"]),
            bracket=bracket,
            metadata=dict(row.get("metadata", {})),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    normalized = str(value).replace("Z", "+00:00")
    return _as_utc(datetime.fromisoformat(normalized))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

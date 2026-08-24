"""Reconcile local bot state with Hyperliquid exchange state.

The reconciler is intentionally fail-closed. If Hyperliquid says a position
exists but local bot state does not match it, the supervisor must not place
blind orders. It should surface a block-level finding for operator review.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.execution.order_manager import V1_TRADE_SYMBOLS
from src.execution.position_supervisor import ManagedPosition, latest_open_eth_managed_position


@dataclass(frozen=True)
class ExchangePosition:
    """Open position parsed from Hyperliquid user state."""

    symbol: str
    side: str
    size_coin: float
    entry_px: float | None = None
    notional_usd: float | None = None
    raw: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExchangeOrder:
    """Open order parsed from Hyperliquid open-order state."""

    symbol: str
    side: str
    size_coin: float
    oid: int | str | None = None
    reduce_only: bool = False
    is_trigger: bool = False
    is_position_tpsl: bool = False
    order_type: str = ""
    trigger_px: float | None = None
    raw: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExchangeSnapshot:
    """Bounded exchange state used for reconciliation."""

    user: str
    account_value: float | None
    positions: tuple[ExchangePosition, ...]
    orders: tuple[ExchangeOrder, ...]
    captured_at: datetime

    def to_dict(self) -> dict:
        out = asdict(self)
        out["captured_at"] = self.captured_at.isoformat()
        return out


@dataclass(frozen=True)
class ReconciliationFinding:
    """A local-vs-exchange reconciliation finding."""

    severity: str
    code: str
    message: str
    symbol: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReconciliationReport:
    """Reconciliation status consumed by canary and supervisor workers."""

    status: str
    action: str
    local_position: ManagedPosition | None
    exchange_snapshot: ExchangeSnapshot | None
    findings: tuple[ReconciliationFinding, ...]
    generated_at: datetime

    @property
    def blocking(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "action": self.action,
            "local_position": asdict(self.local_position) if self.local_position else None,
            "exchange_snapshot": self.exchange_snapshot.to_dict() if self.exchange_snapshot else None,
            "findings": [f.to_dict() for f in self.findings],
            "blocking": self.blocking,
            "generated_at": self.generated_at.isoformat(),
        }


def fetch_exchange_snapshot(info: Any, user: str) -> ExchangeSnapshot:
    """Fetch user positions and open orders through the official SDK Info client."""
    user_state = info.user_state(user)
    if hasattr(info, "frontend_open_orders"):
        raw_orders = info.frontend_open_orders(user)
    else:
        raw_orders = info.open_orders(user)
    spot_state = None
    if hasattr(info, "spot_user_state"):
        try:
            spot_state = info.spot_user_state(user)
        except Exception:
            spot_state = None  # optional enrichment; never fail the snapshot
    return build_exchange_snapshot(user_state, raw_orders, user=user, spot_state=spot_state)


UNIFIED_COLLATERAL_COINS = ("USDC",)


def spot_collateral(spot_state: Any) -> float | None:
    """Total spot collateral, used when the perps view reports nothing.

    Unified accounts share one balance between spot and perps, so
    marginSummary.accountValue reads 0.0 until margin is actually in use.
    Reading only that field reports a funded account as empty, which
    silently defeats the point of reconciliation. Falling back to spot
    USDC gives the real figure. Returns None when unavailable.
    """
    if not isinstance(spot_state, dict):
        return None
    total: float | None = None
    for balance in spot_state.get("balances") or []:
        if not isinstance(balance, dict):
            continue
        if str(balance.get("coin", "")).upper() in UNIFIED_COLLATERAL_COINS:
            value = _float_or_none(balance.get("total"))
            if value is not None:
                total = value if total is None else total + value
    return total


def build_exchange_snapshot(
    user_state: dict,
    open_orders: list[dict],
    *,
    user: str,
    captured_at: datetime | None = None,
    spot_state: dict | None = None,
) -> ExchangeSnapshot:
    """Build a normalized snapshot from raw Hyperliquid SDK responses."""
    margin = user_state.get("marginSummary", {}) if isinstance(user_state, dict) else {}
    account_value = _float_or_none(margin.get("accountValue"))
    if not account_value:
        # Unified account: perps margin reads 0.0 while funds sit in the
        # shared spot balance. Prefer the real number over a phantom zero.
        spot_value = spot_collateral(spot_state)
        if spot_value is not None:
            account_value = spot_value
    raw_positions = user_state.get("assetPositions", []) if isinstance(user_state, dict) else []
    positions = tuple(
        pos for pos in (_parse_exchange_position(row) for row in raw_positions) if pos is not None
    )
    orders = tuple(
        order for order in (_parse_exchange_order(row) for row in open_orders or []) if order is not None
    )
    return ExchangeSnapshot(
        user=user,
        account_value=account_value,
        positions=positions,
        orders=orders,
        captured_at=_as_utc(captured_at or datetime.now(timezone.utc)),
    )


def reconcile(
    *,
    local_position: ManagedPosition | None,
    exchange_snapshot: ExchangeSnapshot | None,
    expected_symbol: str = "ETH",
    size_tolerance: float = 1e-6,
) -> ReconciliationReport:
    """Compare local supervised state with exchange positions and orders."""
    now = datetime.now(timezone.utc)
    findings: list[ReconciliationFinding] = []
    if exchange_snapshot is None:
        findings.append(
            ReconciliationFinding(
                "warn",
                "exchange_snapshot_missing",
                "exchange snapshot not provided; live reconciliation skipped",
            )
        )
        return ReconciliationReport(
            status="skipped",
            action="do_not_live_trade",
            local_position=local_position,
            exchange_snapshot=None,
            findings=tuple(findings),
            generated_at=now,
        )

    expected_symbol = expected_symbol.upper()
    exchange_positions = [p for p in exchange_snapshot.positions if abs(p.size_coin) > 0]
    v1_positions = [p for p in exchange_positions if p.symbol in V1_TRADE_SYMBOLS]
    expected_positions = [p for p in v1_positions if p.symbol == expected_symbol]

    for position in exchange_positions:
        if position.symbol not in V1_TRADE_SYMBOLS:
            findings.append(
                ReconciliationFinding(
                    "block",
                    "unexpected_non_v1_position",
                    f"exchange has non-v1 position {position.symbol}; live bot must stand down",
                    position.symbol,
                )
            )

    if len(v1_positions) > 1:
        findings.append(
            ReconciliationFinding(
                "block",
                "multiple_v1_positions",
                "exchange has multiple v1 positions; current supervisor expects one ETH lane",
            )
        )

    if local_position is None and not expected_positions:
        findings.append(ReconciliationFinding("info", "flat", "local and exchange are flat for expected symbol", expected_symbol))
    elif local_position is None and expected_positions:
        findings.append(
            ReconciliationFinding(
                "block",
                "exchange_position_without_local_state",
                f"exchange has open {expected_symbol} position but local supervisor has none",
                expected_symbol,
            )
        )
    elif local_position is not None and not expected_positions:
        findings.append(
            ReconciliationFinding(
                "block",
                "local_position_missing_on_exchange",
                f"local supervisor expects {local_position.symbol} position but exchange is flat",
                local_position.symbol,
            )
        )
    elif local_position is not None and expected_positions:
        exchange_position = expected_positions[0]
        findings.extend(_compare_local_exchange(local_position, exchange_position, size_tolerance=size_tolerance))

    for position in expected_positions:
        if not has_protective_stop(position, exchange_snapshot.orders):
            findings.append(
                ReconciliationFinding(
                    "block",
                    "protective_stop_missing",
                    f"open {position.symbol} {position.side} position has no matching reduce-only trigger stop",
                    position.symbol,
                )
            )

    blocking = any(f.severity == "block" for f in findings)
    status = "blocked" if blocking else "ok"
    action = "do_not_live_trade" if blocking else "safe_to_supervise"
    return ReconciliationReport(
        status=status,
        action=action,
        local_position=local_position,
        exchange_snapshot=exchange_snapshot,
        findings=tuple(findings),
        generated_at=now,
    )


def build_reconciliation_preview(
    data_dir: Path,
    *,
    exchange_snapshot: ExchangeSnapshot | None = None,
) -> dict:
    """Build a canary-safe reconciliation preview for the current ETH lane."""
    report = reconcile(
        local_position=latest_open_eth_managed_position(data_dir),
        exchange_snapshot=exchange_snapshot,
        expected_symbol="ETH",
    )
    return report.to_dict()


def has_protective_stop(position: ExchangePosition, orders: tuple[ExchangeOrder, ...]) -> bool:
    """Return whether open orders contain a matching reduce-only trigger stop."""
    close_side = "buy" if position.side == "short" else "sell"
    for order in orders:
        if order.symbol != position.symbol:
            continue
        if not order.reduce_only:
            continue
        if order.side != close_side:
            continue
        if not (order.is_trigger or order.is_position_tpsl or order.trigger_px):
            continue
        return True
    return False


def _compare_local_exchange(
    local: ManagedPosition,
    exchange_position: ExchangePosition,
    *,
    size_tolerance: float,
) -> list[ReconciliationFinding]:
    findings: list[ReconciliationFinding] = []
    local_symbol = local.symbol.upper()
    if local_symbol != exchange_position.symbol:
        findings.append(
            ReconciliationFinding(
                "block",
                "symbol_mismatch",
                f"local symbol {local_symbol} does not match exchange symbol {exchange_position.symbol}",
                exchange_position.symbol,
            )
        )
    if local.side.lower() != exchange_position.side:
        findings.append(
            ReconciliationFinding(
                "block",
                "side_mismatch",
                f"local side {local.side} does not match exchange side {exchange_position.side}",
                exchange_position.symbol,
            )
        )
    if abs(float(local.size_coin) - float(exchange_position.size_coin)) > size_tolerance:
        findings.append(
            ReconciliationFinding(
                "block",
                "size_mismatch",
                f"local size {local.size_coin} does not match exchange size {exchange_position.size_coin}",
                exchange_position.symbol,
            )
        )
    if not findings:
        findings.append(
            ReconciliationFinding(
                "info",
                "local_exchange_match",
                "local supervisor position matches exchange position",
                exchange_position.symbol,
            )
        )
    return findings


def _parse_exchange_position(row: dict) -> ExchangePosition | None:
    position = row.get("position", row) if isinstance(row, dict) else {}
    symbol = str(position.get("coin", "")).upper()
    size = _float_or_none(position.get("szi"))
    if not symbol or size is None or abs(size) <= 0:
        return None
    side = "long" if size > 0 else "short"
    entry_px = _float_or_none(position.get("entryPx"))
    notional = _float_or_none(position.get("positionValue"))
    return ExchangePosition(
        symbol=symbol,
        side=side,
        size_coin=abs(size),
        entry_px=entry_px,
        notional_usd=notional,
        raw=row,
    )


def _parse_exchange_order(row: dict) -> ExchangeOrder | None:
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("coin", "")).upper()
    if not symbol:
        return None
    side = _parse_order_side(row.get("side"))
    size = _float_or_none(row.get("sz", row.get("origSz")))
    return ExchangeOrder(
        symbol=symbol,
        side=side,
        size_coin=abs(size or 0.0),
        oid=row.get("oid"),
        reduce_only=bool(row.get("reduceOnly", False)),
        is_trigger=bool(row.get("isTrigger", False)),
        is_position_tpsl=bool(row.get("isPositionTpsl", False)),
        order_type=str(row.get("orderType", "")),
        trigger_px=_float_or_none(row.get("triggerPx")),
        raw=row,
    )


def _parse_order_side(value: Any) -> str:
    side = str(value or "").upper()
    if side == "B":
        return "buy"
    if side == "A":
        return "sell"
    if side in {"BUY", "SELL"}:
        return side.lower()
    return side.lower()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

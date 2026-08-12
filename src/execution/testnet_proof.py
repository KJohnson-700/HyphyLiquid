"""Guarded testnet proof flow for reconciliation and reduce-only close logic."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.execution.position_supervisor import ReduceOnlyCloseIntent, execute_reduce_only_close
from src.execution.pricing import aggressive_ioc_limit_px, round_to_tick
from src.execution.reconciler import ExchangeSnapshot, build_exchange_snapshot, has_protective_stop, reconcile


DEFAULT_ORDER_GROUPING = "normalTpsl"


@dataclass(frozen=True)
class TestnetProofGuard:
    """Whether the requested proof mode is allowed to proceed."""

    allowed: bool
    reason: str
    env: str
    user: str
    fetch_exchange: bool
    execute_orders: bool
    cli_armed: bool

    def to_dict(self) -> dict:
        return asdict(self)


def check_testnet_proof_guard(
    *,
    env: str,
    user: str = "",
    fetch_exchange: bool = False,
    execute_orders: bool = False,
    cli_armed: bool = False,
) -> TestnetProofGuard:
    """Validate the requested testnet proof mode."""
    normalized_env = (env or "").strip().lower()
    user = user.strip()
    if not fetch_exchange and not execute_orders:
        return TestnetProofGuard(True, "dry-run plan only; no exchange calls", normalized_env, user, False, False, cli_armed)
    if normalized_env != "testnet":
        return TestnetProofGuard(False, "proof runner only fetches/executes against testnet", normalized_env, user, fetch_exchange, execute_orders, cli_armed)
    if not user:
        return TestnetProofGuard(False, "wallet user address is required for exchange proof", normalized_env, user, fetch_exchange, execute_orders, cli_armed)
    if execute_orders and not fetch_exchange:
        return TestnetProofGuard(False, "order proof requires --fetch-exchange", normalized_env, user, fetch_exchange, execute_orders, cli_armed)
    if execute_orders and not cli_armed:
        return TestnetProofGuard(False, "--i-understand-testnet-orders is required to place testnet orders", normalized_env, user, fetch_exchange, execute_orders, cli_armed)
    return TestnetProofGuard(True, "testnet proof guard passed", normalized_env, user, fetch_exchange, execute_orders, cli_armed)


def build_testnet_proof_plan(guard: TestnetProofGuard, *, proof_kind: str = "open_close") -> dict:
    """Build a readable proof plan for status artifacts."""
    proof_kind = proof_kind.strip().lower()
    if proof_kind == "bracket":
        execution_steps = [
            "if explicitly armed, submit one tiny testnet ETH IOC entry with reduce-only SL",
            "fetch exchange state after entry and verify a protective stop is visible",
            "submit reduce-only timeout-style close",
            "cancel any leftover protective stop orders",
            "fetch exchange state after cleanup and write status",
        ]
    else:
        proof_kind = "open_close"
        execution_steps = [
            "if explicitly armed, open one tiny testnet ETH IOC position",
            "fetch exchange state after open",
            "submit reduce-only timeout-style close",
            "fetch exchange state after close and write status",
        ]
    steps = [
        "load official Hyperliquid SDK clients",
        "fetch user_state and open orders",
        "normalize exchange snapshot",
        "run local-vs-exchange reconciliation",
    ]
    steps.extend(execution_steps)
    if not guard.fetch_exchange:
        steps = steps[:4]
    if not guard.execute_orders:
        steps = steps[:4]
    return {
        "mode": (
            f"execute_testnet_{proof_kind}"
            if guard.execute_orders
            else "fetch_exchange"
            if guard.fetch_exchange
            else "dry_run"
        ),
        "proof_kind": proof_kind,
        "guard": guard.to_dict(),
        "steps": steps,
        "orders_will_be_sent": guard.execute_orders and guard.allowed,
    }


def run_fetch_only_proof(info: Any, *, user: str) -> dict:
    """Fetch exchange state and run reconciliation without placing orders."""
    snapshot = fetch_snapshot_from_info(info, user=user)
    report = reconcile(local_position=None, exchange_snapshot=snapshot)
    return {
        "mode": "fetch_exchange",
        "snapshot": snapshot.to_dict(),
        "reconciliation": report.to_dict(),
        "orders_sent": [],
    }


def run_order_proof(
    info: Any,
    exchange: Any,
    *,
    user: str,
    symbol: str = "ETH",
    side: str = "short",
    size_coin: float = 0.01,
    slippage_bps: float = 20.0,
) -> dict:
    """Open and close one tiny testnet position with reconciliation checks."""
    before = fetch_snapshot_from_info(info, user=user)
    before_report = reconcile(local_position=None, exchange_snapshot=before, expected_symbol=symbol)
    if before_report.blocking:
        return {
            "mode": "execute_testnet_orders",
            "status": "blocked_before_open",
            "before": before.to_dict(),
            "before_reconciliation": before_report.to_dict(),
            "orders_sent": [],
        }

    mid = _mid_for(info, symbol)
    open_order = build_ioc_order(
        symbol=symbol,
        side=side,
        size_coin=size_coin,
        mark_px=mid,
        reduce_only=False,
        slippage_bps=slippage_bps,
    )
    open_response = exchange.order(
        open_order["name"],
        open_order["is_buy"],
        open_order["sz"],
        open_order["limit_px"],
        open_order["order_type"],
        reduce_only=open_order["reduce_only"],
    )

    after_open = fetch_snapshot_from_info(info, user=user)
    matching = [p for p in after_open.positions if p.symbol == symbol.upper()]
    if not matching:
        return {
            "mode": "execute_testnet_orders",
            "status": "open_not_detected",
            "before": before.to_dict(),
            "after_open": after_open.to_dict(),
            "orders_sent": [{"type": "open_ioc", "request": open_order, "response": open_response}],
        }

    position = matching[0]
    close_intent = ReduceOnlyCloseIntent(
        position_id=f"testnet-proof-{symbol.upper()}",
        symbol=symbol.upper(),
        is_buy=(position.side == "short"),
        size_coin=position.size_coin,
        reason="testnet_proof_reduce_only_close",
    )
    close_result = execute_reduce_only_close(exchange, close_intent, mark_px=_mid_for(info, symbol), slippage_bps=slippage_bps)
    after_close = fetch_snapshot_from_info(info, user=user)
    return {
        "mode": "execute_testnet_orders",
        "status": "close_submitted" if close_result.submitted else "close_failed",
        "before": before.to_dict(),
        "after_open": after_open.to_dict(),
        "after_close": after_close.to_dict(),
        "orders_sent": [
            {"type": "open_ioc", "request": open_order, "response": open_response},
            {"type": "reduce_only_close", "request": close_intent.to_dict(), "response": close_result.to_dict()},
        ],
    }


def run_bracket_proof(
    info: Any,
    exchange: Any,
    *,
    user: str,
    symbol: str = "ETH",
    side: str = "short",
    size_coin: float = 0.01,
    slippage_bps: float = 20.0,
    stop_bps: float = 35.0,
) -> dict:
    """Open a tiny testnet position with a visible reduce-only protective stop."""
    before = fetch_snapshot_from_info(info, user=user)
    before_report = reconcile(local_position=None, exchange_snapshot=before, expected_symbol=symbol)
    if before_report.blocking:
        return {
            "mode": "execute_testnet_bracket",
            "status": "blocked_before_open",
            "before": before.to_dict(),
            "before_reconciliation": before_report.to_dict(),
            "orders_sent": [],
        }

    mid = _mid_for(info, symbol)
    open_order = build_ioc_order(
        symbol=symbol,
        side=side,
        size_coin=size_coin,
        mark_px=mid,
        reduce_only=False,
        slippage_bps=slippage_bps,
    )
    stop_order = build_protective_stop_order(
        symbol=symbol,
        side=side,
        size_coin=size_coin,
        mark_px=mid,
        stop_bps=stop_bps,
    )
    bracket_requests = [_as_bulk_order_request(open_order), _as_bulk_order_request(stop_order)]
    bracket_response = exchange.bulk_orders(bracket_requests, grouping=DEFAULT_ORDER_GROUPING)

    after_open = fetch_snapshot_from_info(info, user=user)
    matching = [p for p in after_open.positions if p.symbol == symbol.upper()]
    if not matching:
        return {
            "mode": "execute_testnet_bracket",
            "status": "open_not_detected",
            "before": before.to_dict(),
            "after_open": after_open.to_dict(),
            "orders_sent": [{"type": "entry_with_stop", "request": bracket_requests, "response": bracket_response}],
        }

    position = matching[0]
    protective_stop_visible = has_protective_stop(position, after_open.orders)
    close_intent = ReduceOnlyCloseIntent(
        position_id=f"testnet-bracket-proof-{symbol.upper()}",
        symbol=symbol.upper(),
        is_buy=(position.side == "short"),
        size_coin=position.size_coin,
        reason="testnet_bracket_proof_reduce_only_close",
    )
    close_result = execute_reduce_only_close(exchange, close_intent, mark_px=_mid_for(info, symbol), slippage_bps=slippage_bps)
    after_close = fetch_snapshot_from_info(info, user=user)
    cleanup_response = cancel_open_symbol_orders(exchange, after_close.orders, symbol=symbol)
    after_cleanup = fetch_snapshot_from_info(info, user=user)
    if not protective_stop_visible:
        status = "protective_stop_missing"
    elif not close_result.submitted:
        status = "close_failed"
    else:
        status = "bracket_proof_passed"
    return {
        "mode": "execute_testnet_bracket",
        "status": status,
        "protective_stop_visible": protective_stop_visible,
        "before": before.to_dict(),
        "after_open": after_open.to_dict(),
        "after_close": after_close.to_dict(),
        "after_cleanup": after_cleanup.to_dict(),
        "cleanup_response": cleanup_response,
        "orders_sent": [
            {"type": "entry_with_stop", "request": bracket_requests, "response": bracket_response},
            {"type": "reduce_only_close", "request": close_intent.to_dict(), "response": close_result.to_dict()},
        ],
    }


def fetch_snapshot_from_info(info: Any, *, user: str) -> ExchangeSnapshot:
    """Fetch an exchange snapshot using SDK Info methods."""
    user_state = info.user_state(user)
    if hasattr(info, "frontend_open_orders"):
        open_orders = info.frontend_open_orders(user)
    else:
        open_orders = info.open_orders(user)
    return build_exchange_snapshot(user_state, open_orders, user=user)


def build_ioc_order(
    *,
    symbol: str,
    side: str,
    size_coin: float,
    mark_px: float,
    reduce_only: bool,
    slippage_bps: float = 20.0,
) -> dict:
    """Build an aggressive IOC order request compatible with the SDK."""
    if size_coin <= 0:
        raise ValueError("size_coin must be positive")
    if mark_px <= 0:
        raise ValueError("mark_px must be positive")
    normalized_side = side.lower()
    if normalized_side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    is_buy = normalized_side == "long"
    limit_px = aggressive_ioc_limit_px(symbol, mark_px, is_buy, slippage_bps)
    return {
        "name": symbol.upper(),
        "is_buy": is_buy,
        "sz": size_coin,
        "limit_px": limit_px,
        "order_type": {"limit": {"tif": "Ioc"}},
        "reduce_only": reduce_only,
    }


def build_protective_stop_order(
    *,
    symbol: str,
    side: str,
    size_coin: float,
    mark_px: float,
    stop_bps: float = 35.0,
) -> dict:
    """Build a reduce-only stop-market order for an already-open side."""
    if size_coin <= 0:
        raise ValueError("size_coin must be positive")
    if mark_px <= 0:
        raise ValueError("mark_px must be positive")
    normalized_side = side.lower()
    if normalized_side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    stop_fraction = max(0.0, stop_bps) / 10_000.0
    is_close_buy = normalized_side == "short"
    raw_stop = mark_px * (1.0 + stop_fraction) if is_close_buy else mark_px * (1.0 - stop_fraction)
    stop_px = round_to_tick(symbol, raw_stop)
    return {
        "name": symbol.upper(),
        "is_buy": is_close_buy,
        "sz": size_coin,
        "limit_px": stop_px,
        "order_type": {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}},
        "reduce_only": True,
    }


def cancel_open_symbol_orders(exchange: Any, orders: tuple, *, symbol: str) -> dict:
    """Best-effort cancel for leftover testnet protective orders."""
    cancel_requests = [
        {"coin": order.symbol, "oid": order.oid}
        for order in orders
        if order.symbol == symbol.upper() and order.oid is not None
    ]
    if not cancel_requests:
        return {"attempted": False, "requests": [], "response": None, "error": None}
    try:
        response = exchange.bulk_cancel(cancel_requests)
        return {"attempted": True, "requests": cancel_requests, "response": response, "error": None}
    except Exception as exc:
        return {"attempted": True, "requests": cancel_requests, "response": None, "error": str(exc)}


def _as_bulk_order_request(order: dict) -> dict:
    """Convert local order shape into the SDK bulk_orders request shape."""
    out = dict(order)
    if "coin" not in out:
        out["coin"] = out.get("name")
    out.pop("name", None)
    return out


def _mid_for(info: Any, symbol: str) -> float:
    mids = info.all_mids()
    mid = float(mids[symbol.upper()])
    if mid <= 0:
        raise ValueError(f"invalid mid for {symbol}: {mid}")
    return mid


def utc_now_iso() -> str:
    """Return current UTC timestamp as an ISO string."""
    return datetime.now(timezone.utc).isoformat()

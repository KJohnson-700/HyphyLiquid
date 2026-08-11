"""Convert vetted paper positions into execution bracket intents."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.execution.order_manager import BracketOrderIntent, V1_TRADE_SYMBOLS
from src.execution.paper_broker import PaperBracket, PaperPosition

ACTIVE_EXECUTION_LANE = "eth_funding_context_follow"


def paper_position_to_bracket_intent(position: PaperPosition) -> BracketOrderIntent:
    """Convert the active ETH paper lane into an execution-ready bracket intent.

    The current ETH candidate is a stop-only bracket with a bot-managed
    60-minute time exit. That means this intent is necessary but not sufficient
    for live autonomy; the canary/status layer must still supervise timeout
    exits.
    """
    symbol = position.symbol.upper()
    if position.paper_scope != "v1_paper":
        raise ValueError("only v1_paper positions can become execution intents")
    if symbol not in V1_TRADE_SYMBOLS:
        raise ValueError(f"{symbol} is not in the v1 execution allowlist")
    if position.lane != ACTIVE_EXECUTION_LANE:
        raise ValueError(f"paper lane {position.lane} is not the active execution lane")
    if symbol != "ETH" or position.direction != "short":
        raise ValueError("active execution lane must be ETH short")
    if position.bracket.target_price is not None:
        raise ValueError("ETH funding follow is currently stop-only; target must be None")
    if position.bracket.activation_price is not None or position.bracket.trail_bps is not None:
        raise ValueError("ETH funding follow does not use trailing activation in v1")

    return BracketOrderIntent(
        signal_ts=pd.Timestamp(position.entry_ts),
        symbol=symbol,
        side=position.direction,
        entry_px=float(position.entry_price),
        sl_px=float(position.bracket.initial_stop_price),
        tp_px=None,
        notional_usd=float(position.notional_usd),
        reason=(
            f"{ACTIVE_EXECUTION_LANE}; paper_id={position.paper_id}; "
            f"max_hold_minutes={position.bracket.max_hold_minutes}; "
            "timeout_exit=bot_managed"
        ),
    )


def latest_active_eth_position(data_dir: Path) -> PaperPosition | None:
    """Return the latest still-open active ETH paper position from append-only ledgers."""
    latest: PaperPosition | None = None
    rows_by_path: list[dict] = []
    for path in sorted(data_dir.glob("paper_positions_*.jsonl")):
        rows_by_path.extend(_load_jsonl(path))
    closed_ids = {
        str(row.get("paper_id"))
        for row in rows_by_path
        if row.get("event") == "mark"
        and isinstance(row.get("fill"), dict)
        and row["fill"].get("status") == "closed"
    }
    for row in rows_by_path:
        if row.get("event") != "opened":
            continue
        if str(row.get("paper_id")) in closed_ids:
            continue
        if row.get("symbol") != "ETH" or row.get("lane") != ACTIVE_EXECUTION_LANE:
            continue
        position = _position_from_row(row)
        if position is not None:
            latest = position
    return latest


def build_latest_eth_intent_preview(data_dir: Path) -> dict:
    """Build a canary-safe preview of the latest ETH execution intent."""
    position = latest_active_eth_position(data_dir)
    if position is None:
        return {
            "eligible": False,
            "reason": "no open ETH funding-context paper position is available",
            "lane": ACTIVE_EXECUTION_LANE,
        }
    try:
        intent = paper_position_to_bracket_intent(position)
    except ValueError as exc:
        return {
            "eligible": False,
            "reason": str(exc),
            "lane": position.lane,
            "paper_id": position.paper_id,
        }
    return {
        "eligible": True,
        "reason": "latest ETH paper position can be represented as a stop-only bracket intent",
        "paper_id": position.paper_id,
        "lane": position.lane,
        "symbol": intent.symbol,
        "side": intent.side,
        "entry_px": intent.entry_px,
        "sl_px": intent.sl_px,
        "tp_px": intent.tp_px,
        "notional_usd": intent.notional_usd,
        "max_hold_minutes": position.bracket.max_hold_minutes,
        "timeout_exit": "bot_managed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                import json

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

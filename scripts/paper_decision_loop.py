"""Live-like paper decision loop for BTC/HYPE liquidation lanes.

This loop consumes the same local live data files used by the rebuild flow:
`data/cascades.jsonl` and `data/ws_candle/*.jsonl`. It never imports the real
order manager or exchange client; only the final broker/fill boundary is fake.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.execution.paper_broker import PaperBracket, PaperFill, PaperPosition, mark_position
from src.risk import RiskConfig, RiskManager, RiskState, RiskVerdict
from src.strategy.fade_or_follow_backtest import (
    _bar_dt,
    _continuation_direction,
    _fade_direction,
    find_entry_idx,
)
from src.strategy.lane_backtest import _range_confirmation, bollinger_at
from src.strategy.regime import classify_candle_regime, classify_liquidation_response, route_signal

LOG = logging.getLogger("paper_decision_loop")

DATA_DIR = PROJECT_ROOT / "data"
STATE_PATH = DATA_DIR / ".paper_decision_state.json"

PAPER_SYMBOLS = {"BTC", "HYPE"}
BTC_WAIT_MINUTES = 3
BTC_MAX_HOLD_MINUTES = 240
BTC_EVENT_VWAP_BUFFER_BPS = 15.0
BTC_ACTIVATION_R = 2.0
BTC_TRAIL_BPS = 10.0

HYPE_MAX_HOLD_MINUTES = 15
HYPE_STOP_BUFFER_BPS = 5.0

ROUND_TRIP_COST_BPS = 8.0
STOP_SLIPPAGE_BPS = 2.0
MAX_PAPER_NOTIONAL_USD = 10_000.0
TARGET_RISK_USD = 10.0
MAX_ENTRY_LAG_MINUTES = 2


@dataclass(frozen=True)
class PaperDecision:
    """One deterministic paper-routing decision."""

    decision_ts: str
    cascade_key: str
    symbol: str
    side: str
    lane: str
    action: str
    paper_scope: str
    execution_allowed: bool
    route_reason: str
    candle_regime: dict
    liquidation_response: dict
    decision: str
    reason: str
    paper_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_path(prefix: str, *, data_dir: Path = DATA_DIR, now: datetime | None = None) -> Path:
    dt = now or _utc_now()
    return data_dir / f"{prefix}_{dt.strftime('%Y%m%d')}.jsonl"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"processed_cascades": [], "closed_positions": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed_cascades": [], "closed_positions": []}


def _save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _bar_ms(bar: dict) -> int | None:
    payload = bar.get("payload", {}) if isinstance(bar.get("payload"), dict) else {}
    try:
        return int(bar.get("t", payload.get("t")))
    except (TypeError, ValueError):
        return None


def _bar_end_ms(bar: dict) -> int | None:
    payload = bar.get("payload", {}) if isinstance(bar.get("payload"), dict) else {}
    try:
        return int(bar.get("T", payload.get("T")))
    except (TypeError, ValueError):
        start = _bar_ms(bar)
        return start + 59_999 if start is not None else None


def _close(bar: dict) -> float | None:
    payload = bar.get("payload", {}) if isinstance(bar.get("payload"), dict) else {}
    try:
        return float(bar.get("c", payload.get("c")))
    except (TypeError, ValueError):
        return None


def _load_completed_candles(symbol: str, *, data_dir: Path, now_ms: int | None = None) -> list[dict]:
    candle_dir = data_dir / "ws_candle"
    if not candle_dir.exists():
        return []
    cutoff = now_ms if now_ms is not None else int(_utc_now().timestamp() * 1000)
    latest_by_open: dict[int, dict] = {}
    for path in sorted(candle_dir.glob(f"{symbol.lower()}_*.jsonl")):
        for row in _load_jsonl(path):
            bar = row.get("payload", row)
            if not isinstance(bar, dict):
                continue
            start = _bar_ms(bar)
            end = _bar_end_ms(bar)
            if start is None or end is None or end > cutoff:
                continue
            latest_by_open[start] = bar
    return [latest_by_open[k] for k in sorted(latest_by_open)]


def _candles_until(candles: list[dict], cutoff_ms: int) -> list[dict]:
    """Return completed candles whose close time is not after cutoff_ms."""
    out = []
    for candle in candles:
        end = _bar_end_ms(candle)
        if end is not None and end <= cutoff_ms:
            out.append(candle)
    return out


def _load_cascades(data_dir: Path) -> list[dict]:
    return _load_jsonl(data_dir / "cascades.jsonl")


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


def _load_open_positions(data_dir: Path, closed_ids: set[str]) -> dict[str, PaperPosition]:
    """Recover open paper positions from append-only paper ledgers."""
    positions: dict[str, PaperPosition] = {}
    for path in sorted(data_dir.glob("paper_positions_*.jsonl")):
        for row in _load_jsonl(path):
            event = row.get("event")
            paper_id = row.get("paper_id")
            if not paper_id:
                continue
            fill = row.get("fill", {})
            if event == "mark" and isinstance(fill, dict) and fill.get("status") == "closed":
                closed_ids.add(str(paper_id))
                positions.pop(str(paper_id), None)
                continue
            if event == "opened" and str(paper_id) not in closed_ids:
                pos = _position_from_row(row)
                if pos is not None:
                    positions[pos.paper_id] = pos
    return positions


def _cascade_key(cascade: dict) -> str:
    sym = str(cascade.get("symbol", "")).upper()
    side = str(cascade.get("side", ""))
    ts = str(cascade.get("start_ts", cascade.get("event_ts", "")))
    n = str(cascade.get("n_fills", ""))
    notional = str(round(float(cascade.get("total_notional", 0) or 0), 2))
    return f"{sym}|{side}|{ts}|{n}|{notional}"


def _cascade_ts_ms(cascade: dict) -> int | None:
    ts = cascade.get("event_ts_ms")
    if isinstance(ts, (int, float)):
        return int(ts)
    ts_text = cascade.get("start_ts", cascade.get("event_ts"))
    try:
        dt = datetime.fromisoformat(str(ts_text))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _paper_id(cascade_key: str, lane: str) -> str:
    digest = hashlib.sha256(f"{cascade_key}|{lane}".encode("utf-8")).hexdigest()[:16]
    return f"paper-{digest}"


def _price_with_bps(price: float, direction: str, bps: float) -> float:
    pct = bps / 10_000.0
    if direction == "long":
        return price * (1.0 + pct)
    return price * (1.0 - pct)


def _paper_notional_for_stop(entry: float, stop: float) -> tuple[float, float]:
    if entry <= 0 or stop <= 0:
        return 0.0, 0.0
    stop_pct = abs(entry - stop) / entry
    if stop_pct <= 0:
        return 0.0, 0.0
    notional = min(MAX_PAPER_NOTIONAL_USD, TARGET_RISK_USD / stop_pct)
    risk_usd = notional * stop_pct
    return notional, risk_usd


def _risk_verdict(symbol: str, direction: str, notional_usd: float, risk_usd: float) -> RiskVerdict:
    rm = RiskManager(RiskConfig(), RiskState())
    return rm.check_trade(
        symbol=symbol,
        side=direction,
        size_usd=notional_usd,
        leverage=1.0,
        stop_distance_usd=risk_usd,
    )


def _build_btc_position(
    cascade: dict,
    candles: list[dict],
    entry_idx: int,
    response_closes: Iterable[float],
) -> tuple[PaperDecision, PaperPosition | None]:
    key = _cascade_key(cascade)
    sym = str(cascade.get("symbol", "")).upper()
    side = str(cascade.get("side", ""))
    event_vwap = float(cascade.get("event_vwap", 0) or 0)
    candle_regime = classify_candle_regime(candles, entry_idx)
    response = classify_liquidation_response(side, event_vwap, response_closes, wait_minutes=BTC_WAIT_MINUTES)
    route = route_signal(sym, side, candle_regime, response)
    base = {
        "decision_ts": _utc_now().isoformat(),
        "cascade_key": key,
        "symbol": sym,
        "side": side,
        "lane": route.lane,
        "action": route.action,
        "paper_scope": "v1_paper" if route.execution_allowed and route.action == "watch" else "none",
        "execution_allowed": route.execution_allowed,
        "route_reason": route.reason,
        "candle_regime": candle_regime.to_dict(),
        "liquidation_response": response.to_dict(),
    }
    if route.action != "watch" or route.lane != "btc_eth_trailing_resolution":
        return PaperDecision(**base, decision="reject", reason=route.reason), None

    trade_entry_idx = min(entry_idx + BTC_WAIT_MINUTES - 1, len(candles) - 1)
    entry_price = _close(candles[trade_entry_idx])
    if entry_price is None or entry_price <= 0:
        return PaperDecision(**base, decision="reject", reason="missing BTC entry close"), None
    direction = _continuation_direction(side)
    if direction != "long":
        return PaperDecision(**base, decision="reject", reason="BTC paper currently only accepts B-side long continuation"), None
    stop = event_vwap * (1.0 - BTC_EVENT_VWAP_BUFFER_BPS / 10_000.0)
    if stop >= entry_price:
        return PaperDecision(**base, decision="reject", reason="event_vwap stop is not below long entry"), None
    stop_bps = (entry_price - stop) / entry_price * 10_000.0
    activation = _price_with_bps(entry_price, direction, stop_bps * BTC_ACTIVATION_R)
    notional_usd, risk_usd = _paper_notional_for_stop(entry_price, stop)
    verdict = _risk_verdict(sym, direction, notional_usd, risk_usd)
    if verdict != RiskVerdict.APPROVED:
        return PaperDecision(**base, decision="risk_reject", reason=verdict.value), None

    paper_id = _paper_id(key, route.lane)
    position = PaperPosition(
        paper_id=paper_id,
        paper_scope="v1_paper",
        cascade_key=key,
        symbol=sym,
        side=side,
        lane=route.lane,
        direction=direction,
        event_ts=str(cascade.get("start_ts")),
        entry_ts=_bar_dt(candles[trade_entry_idx]).isoformat(),
        entry_idx=trade_entry_idx,
        entry_price=round(entry_price, 8),
        notional_usd=round(notional_usd, 4),
        risk_usd=round(risk_usd, 4),
        bracket=PaperBracket(
            entry_price=round(entry_price, 8),
            initial_stop_price=round(stop, 8),
            target_price=None,
            activation_price=round(activation, 8),
            trail_bps=BTC_TRAIL_BPS,
            max_hold_minutes=BTC_MAX_HOLD_MINUTES,
            stop_slippage_bps=STOP_SLIPPAGE_BPS,
            round_trip_cost_bps=ROUND_TRIP_COST_BPS,
        ),
        metadata={
            "event_vwap": event_vwap,
            "wait_minutes": BTC_WAIT_MINUTES,
            "initial_stop_bps": round(stop_bps, 4),
            "activation_r": BTC_ACTIVATION_R,
        },
    )
    return PaperDecision(**base, decision="open_position", reason="BTC B-side failed-reclaim continuation", paper_id=paper_id), position


def _build_hype_position(cascade: dict, candles: list[dict], entry_idx: int) -> tuple[PaperDecision, PaperPosition | None]:
    key = _cascade_key(cascade)
    sym = str(cascade.get("symbol", "")).upper()
    side = str(cascade.get("side", ""))
    event_vwap = float(cascade.get("event_vwap", 0) or 0)
    candle_regime = classify_candle_regime(candles, entry_idx)
    response = classify_liquidation_response(side, event_vwap, [_close(candles[entry_idx]) or 0.0], wait_minutes=1)
    route = route_signal(sym, side, candle_regime, response)
    base = {
        "decision_ts": _utc_now().isoformat(),
        "cascade_key": key,
        "symbol": sym,
        "side": side,
        "lane": route.lane,
        "action": route.action,
        "paper_scope": "research_paper" if route.action == "research_candidate" else "none",
        "execution_allowed": False,
        "route_reason": route.reason,
        "candle_regime": candle_regime.to_dict(),
        "liquidation_response": response.to_dict(),
    }
    if route.action != "research_candidate":
        return PaperDecision(**base, decision="reject", reason=route.reason), None
    bands = bollinger_at(candles, entry_idx, period=20, stdev_mult=2.0)
    if bands is None or not _range_confirmation(side, candles[entry_idx], bands):
        return PaperDecision(**base, decision="reject", reason="missing HYPE band-extreme confirmation"), None
    entry_price = _close(candles[entry_idx])
    if entry_price is None or entry_price <= 0:
        return PaperDecision(**base, decision="reject", reason="missing HYPE entry close"), None
    direction = _fade_direction(side)
    stop = bands["upper"] * (1.0 + HYPE_STOP_BUFFER_BPS / 10_000.0) if direction == "short" else bands["lower"] * (1.0 - HYPE_STOP_BUFFER_BPS / 10_000.0)
    target = bands["mid"]
    notional_usd, risk_usd = _paper_notional_for_stop(entry_price, stop)
    verdict = _risk_verdict(sym, direction, notional_usd, risk_usd)
    if verdict != RiskVerdict.APPROVED:
        return PaperDecision(**base, decision="risk_reject", reason=verdict.value), None
    paper_id = _paper_id(key, route.lane)
    position = PaperPosition(
        paper_id=paper_id,
        paper_scope="research_paper",
        cascade_key=key,
        symbol=sym,
        side=side,
        lane=route.lane,
        direction=direction,
        event_ts=str(cascade.get("start_ts")),
        entry_ts=_bar_dt(candles[entry_idx]).isoformat(),
        entry_idx=entry_idx,
        entry_price=round(entry_price, 8),
        notional_usd=round(notional_usd, 4),
        risk_usd=round(risk_usd, 4),
        bracket=PaperBracket(
            entry_price=round(entry_price, 8),
            initial_stop_price=round(stop, 8),
            target_price=round(target, 8),
            activation_price=None,
            trail_bps=None,
            max_hold_minutes=HYPE_MAX_HOLD_MINUTES,
            stop_slippage_bps=STOP_SLIPPAGE_BPS,
            round_trip_cost_bps=ROUND_TRIP_COST_BPS,
        ),
        metadata={
            "band_mid": round(bands["mid"], 8),
            "band_upper": round(bands["upper"], 8),
            "band_lower": round(bands["lower"], 8),
            "band_width_pct": round(bands["width_pct"], 4),
        },
    )
    return PaperDecision(**base, decision="open_position", reason="HYPE B-side range/liquidation scalp", paper_id=paper_id), position


def build_position_for_cascade(cascade: dict, candles_by_symbol: dict[str, list[dict]]) -> tuple[PaperDecision | None, PaperPosition | None]:
    """Build a deterministic paper decision and optional paper position."""
    sym = str(cascade.get("symbol", "")).upper()
    if sym not in PAPER_SYMBOLS:
        return None, None
    side = cascade.get("side")
    start_ts = cascade.get("start_ts")
    event_vwap = float(cascade.get("event_vwap", 0) or 0)
    if side not in {"A", "B"} or not start_ts or event_vwap <= 0:
        return None, None
    candles = candles_by_symbol.get(sym, [])
    entry_idx = find_entry_idx(candles, start_ts, MAX_ENTRY_LAG_MINUTES)
    if entry_idx is None:
        return None, None
    if sym == "BTC":
        needed = entry_idx + BTC_WAIT_MINUTES
        if needed > len(candles):
            return None, None
        closes = [_close(c) for c in candles[entry_idx:needed]]
        if any(c is None for c in closes):
            return None, None
        return _build_btc_position(cascade, candles, entry_idx, [float(c) for c in closes if c is not None])
    return _build_hype_position(cascade, candles, entry_idx)


def run_once(*, data_dir: Path = DATA_DIR, state_path: Path = STATE_PATH, max_new: int = 250) -> dict:
    """Process new mature cascades once and update paper ledgers."""
    state = _load_state(state_path)
    processed = set(state.get("processed_cascades", []))
    closed = set(state.get("closed_positions", []))
    open_positions = _load_open_positions(data_dir, closed)
    cascades = _load_cascades(data_dir)
    symbols = {str(c.get("symbol", "")).upper() for c in cascades if str(c.get("symbol", "")).upper() in PAPER_SYMBOLS}
    candles_by_symbol = {sym: _load_completed_candles(sym, data_dir=data_dir) for sym in symbols}
    decisions_written = 0
    positions_opened = 0
    positions_closed = 0

    def mark_open_positions(cutoff_ms: int | None = None) -> None:
        nonlocal positions_closed
        for position in list(open_positions.values()):
            if position.paper_id in closed:
                continue
            symbol_candles = candles_by_symbol.get(position.symbol, [])
            mark_candles = _candles_until(symbol_candles, cutoff_ms) if cutoff_ms is not None else symbol_candles
            fill = mark_position(position, mark_candles)
            if fill.status != "closed":
                continue
            _append_jsonl(
                _day_path("paper_positions", data_dir=data_dir),
                {"event": "mark", **position.to_dict(), "fill": fill.to_dict()},
            )
            closed.add(position.paper_id)
            open_positions.pop(position.paper_id, None)
            positions_closed += 1

    new_cascades = [c for c in cascades[-max_new:] if _cascade_key(c) not in processed]
    new_cascades.sort(key=lambda c: _cascade_ts_ms(c) or 0)
    for cascade in new_cascades:
        mark_open_positions(_cascade_ts_ms(cascade))
        key = _cascade_key(cascade)
        decision, position = build_position_for_cascade(cascade, candles_by_symbol)
        if decision is None:
            continue
        processed.add(key)
        if position is not None:
            if len(open_positions) >= RiskConfig().max_open_positions:
                decision = replace(decision, decision="risk_reject", reason="paper max open positions reached", paper_id=None)
                position = None
        _append_jsonl(_day_path("paper_decisions", data_dir=data_dir), decision.to_dict())
        decisions_written += 1
        if position is not None:
            _append_jsonl(_day_path("paper_positions", data_dir=data_dir), {"event": "opened", **position.to_dict()})
            positions_opened += 1
            open_positions[position.paper_id] = position

    mark_open_positions()
    for position in list(open_positions.values()):
        fill = mark_position(position, candles_by_symbol.get(position.symbol, []))
        if fill.status == "closed":
            continue
        _append_jsonl(
            _day_path("paper_positions", data_dir=data_dir),
            {"event": "mark", **position.to_dict(), "fill": fill.to_dict()},
        )

    state["processed_cascades"] = sorted(processed)[-10_000:]
    state["closed_positions"] = sorted(closed)[-10_000:]
    state["updated_at"] = _utc_now().isoformat()
    _save_state(state, state_path)
    return {
        "decisions_written": decisions_written,
        "positions_opened": positions_opened,
        "positions_closed": positions_closed,
        "processed_total": len(state["processed_cascades"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one pass and exit.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")
    parser.add_argument("--max-new", type=int, default=250, help="Max recent cascades to scan per pass.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    while True:
        result = run_once(max_new=args.max_new)
        LOG.info("paper decision pass: %s", result)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

"""
HyphyLiquid - Order manager.

Takes a CascadeSignal, validates against risk.py, sizes the position,
and places the entry + take-profit + stop-loss atomically via
bulk_orders with grouping="normalTpsl".

The atomic placement matters: if the entry fails, neither TP nor SL
exist. If entry fills, both TP and SL are live in the same block.

v1 TRADING ALLOWLIST
--------------------
v1 only trades BTC and ETH. Other symbols (SOL, HYPE, DOGE, BNB) are
in the passive data collection / research phase per the spec split
(see AGENTS.md). OrderManager.execute() will REFUSE to place orders
for any non-v1 symbol — it's a hard guard, not a soft warning.

Usage:
    from src.execution.order_manager import OrderManager
    mgr = OrderManager.from_env()
    result = mgr.execute(signal, candles, current_price=...)
    # result.filled: bool
    # result.entry_oid, tp_oid, sl_oid
    # result.error if not filled
"""
from __future__ import annotations

# v1 trading allowlist. OrderManager refuses to execute on anything
# not in this set. Other symbols (SOL/HYPE/DOGE/BNB) are research-only.
V1_TRADE_SYMBOLS: frozenset[str] = frozenset({"BTC", "ETH"})
RESEARCH_SYMBOLS: frozenset[str] = frozenset({"SOL", "HYPE", "DOGE", "BNB", "xyz:GOLD", "xyz:SILVER"})

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs) -> bool:
        """Fallback when python-dotenv is unavailable outside live env loading."""
        return False

from src.risk import (
    RiskConfig,
    RiskManager,
    RiskState,
    RiskVerdict,
)
from src.execution.pricing import round_to_tick
from src.strategy.cascade import CascadeSignal, SignalDirection

logger = logging.getLogger(__name__)

DEFAULT_ORDER_GROUPING = "normalTpsl"


@dataclass
class BracketOrderIntent:
    """Execution-ready bracket intent from a deterministic signal lane."""

    signal_ts: pd.Timestamp
    symbol: str
    side: str  # "long" or "short"
    entry_px: float
    sl_px: float
    tp_px: Optional[float] = None
    notional_usd: Optional[float] = None
    reason: str = ""


@dataclass
class OrderResult:
    signal_ts: pd.Timestamp
    symbol: str
    side: str
    requested_size_usd: float
    requested_leverage: float
    entry_px: float
    tp_px: float
    sl_px: float
    risk_verdict: RiskVerdict
    filled: bool
    status: str = "rejected"
    size_coin: float = 0.0
    needs_reconciliation: bool = False
    cancel_attempted: bool = False
    error: Optional[str] = None
    entry_oid: Optional[int] = None
    tp_oid: Optional[int] = None
    sl_oid: Optional[int] = None
    entry_status: Optional[dict] = None
    tp_status: Optional[dict] = None
    sl_status: Optional[dict] = None
    cancel_response: Optional[dict] = None
    response: Optional[dict] = None


class OrderManager:
    def __init__(self, exchange, info, bankroll: float,
                 risk_per_trade_pct: float = 0.01,
                 max_leverage: float = 10.0,
                 tp_atr_multiple: float = 2.0,
                 sl_atr_multiple: float = 1.0,
                 env: str = "testnet",
                 risk_state: Optional[RiskState] = None,
                 order_grouping: str = DEFAULT_ORDER_GROUPING):
        self.exchange = exchange
        self.info = info
        self.bankroll = bankroll
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_leverage = max_leverage
        self.tp_atr_multiple = tp_atr_multiple
        self.sl_atr_multiple = sl_atr_multiple
        self.env = env
        self.order_grouping = order_grouping
        self._meta_cache: Optional[dict] = None
        self._risk_cfg = RiskConfig(
            bankroll_usd=bankroll,
            max_risk_per_trade_pct=risk_per_trade_pct,
            max_leverage=max_leverage,
        )
        self._risk_state = risk_state or RiskState(
            bankroll_at_session_start=bankroll,
        )
        self._risk = RiskManager(self._risk_cfg, self._risk_state)

    @classmethod
    def from_env(cls, env: Optional[str] = None) -> "OrderManager":
        project_root = Path(__file__).resolve().parent.parent.parent
        load_dotenv(project_root / ".env")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        env = env or os.getenv("HYPERLIQUID_ENV", "testnet").lower()
        base_url = ("https://api.hyperliquid-testnet.xyz" if env == "testnet"
                    else "https://api.hyperliquid.xyz")
        pk = os.getenv("HYPERLIQUID_PRIVATE_KEY", "").strip()
        addr = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "").strip()
        if not pk or not addr:
            raise RuntimeError("HYPERLIQUID_PRIVATE_KEY and HYPERLIQUID_WALLET_ADDRESS must be set in .env")
        if not pk.startswith("0x"):
            pk = "0x" + pk
        wallet = Account.from_key(pk)
        if wallet.address.lower() != addr.lower():
            raise RuntimeError(f"Private key derives to {wallet.address}, .env says {addr}")
        info = Info(base_url, skip_ws=True)
        exchange = Exchange(wallet, base_url)
        bankroll = float(os.getenv("HYPERLIQUID_BANKROLL", "1000"))
        max_lev = float(os.getenv("HYPERLIQUID_MAX_LEVERAGE", "10"))
        risk_pct = float(os.getenv("HYPERLIQUID_MAX_RISK_PCT", "0.01"))
        return cls(exchange, info, bankroll, risk_pct, max_lev, env=env)

    def _atr(self, candles: pd.DataFrame, window: int = 14) -> float:
        """Compute ATR (Average True Range) for sizing TP/SL."""
        if candles.empty or len(candles) < window + 1:
            return 0.0
        c = candles.copy()
        c["tr"] = pd.concat([
            c["high"] - c["low"],
            (c["high"] - c["close"].shift(1)).abs(),
            (c["low"] - c["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        return float(c["tr"].tail(window).mean())

    def _size_position(self, entry: float, sl_distance: float) -> tuple[float, float]:
        """Return (size_in_coin, notional_usd). Risk = bankroll * risk_pct."""
        if sl_distance <= 0:
            return 0.0, 0.0
        risk_usd = self.bankroll * self.risk_per_trade_pct
        notional = risk_usd / (sl_distance / entry)
        max_notional = self.bankroll * self.max_leverage
        notional = min(notional, max_notional)
        return notional / entry, notional

    def _round_to_tick(self, symbol: str, price: float) -> float:
        # v1 only: BTC and ETH. Other symbols (SOL/HYPE/DOGE/BNB) are
        # research-only per the spec; OrderManager refuses to trade them
        # (see _v1_allowlist below).
        return round_to_tick(symbol, price)

    def _asset_meta(self, symbol: str) -> dict:
        """Return Hyperliquid metadata for a symbol when available."""
        if self._meta_cache is None:
            try:
                self._meta_cache = self.info.meta()
            except Exception:
                logger.warning("could not load Hyperliquid meta; using size fallbacks", exc_info=True)
                self._meta_cache = {}
        for asset in self._meta_cache.get("universe", []):
            if asset.get("name") == symbol:
                return asset
        return {}

    def _round_size(self, symbol: str, size: float) -> float:
        asset = self._asset_meta(symbol)
        if "szDecimals" in asset:
            return round(size, int(asset["szDecimals"]))
        decimals = {"BTC": 5, "ETH": 4}
        d = decimals.get(symbol, 3)
        return round(size, d)

    def _cancel_entry_if_possible(self, symbol: str, entry_oid: Optional[int]) -> tuple[bool, Optional[dict], Optional[str]]:
        """Try to cancel a resting parent order after child TP/SL failure."""
        if entry_oid is None:
            return False, None, None
        try:
            resp = self.exchange.bulk_cancel([{"coin": symbol, "oid": entry_oid}])
            return True, resp, None
        except Exception as e:
            logger.exception("failed to cancel orphan entry order")
            return True, None, str(e)

    def execute_bracket_intent(self, intent: BracketOrderIntent) -> OrderResult:
        """Execute a precomputed bracket intent.

        This is the live boundary for paper-to-live promotion. The strategy
        lane owns entry, stop, target/trailing thesis, and notional proposal;
        the order manager owns v1 symbol allowlist, tick/size rounding, risk,
        and atomic bracket submission.
        """
        symbol = intent.symbol.upper()
        side = intent.side.lower()
        if symbol not in V1_TRADE_SYMBOLS:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=0.0,
                requested_leverage=0.0,
                entry_px=0.0,
                tp_px=0.0,
                sl_px=0.0,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False,
                status="rejected_v1_allowlist",
                error=(f"{symbol} is not in v1 allowlist "
                       f"({sorted(V1_TRADE_SYMBOLS)}); research-only"),
            )
        if side not in {"long", "short"}:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=0.0,
                requested_leverage=0.0,
                entry_px=0.0,
                tp_px=0.0,
                sl_px=0.0,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False,
                status="rejected",
                error="intent side must be long or short",
            )
        if intent.entry_px <= 0 or intent.sl_px <= 0:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=0.0,
                requested_leverage=0.0,
                entry_px=0.0,
                tp_px=0.0,
                sl_px=0.0,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False,
                status="rejected",
                error="entry_px and sl_px must be positive",
            )

        is_buy = side == "long"
        entry_r = self._round_to_tick(symbol, intent.entry_px)
        sl_r = self._round_to_tick(symbol, intent.sl_px)
        if is_buy and sl_r >= entry_r:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=0.0,
                requested_leverage=0.0,
                entry_px=entry_r,
                tp_px=0.0,
                sl_px=sl_r,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False,
                status="rejected",
                error="long stop must be below entry",
            )
        if not is_buy and sl_r <= entry_r:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=0.0,
                requested_leverage=0.0,
                entry_px=entry_r,
                tp_px=0.0,
                sl_px=sl_r,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False,
                status="rejected",
                error="short stop must be above entry",
            )

        stop_distance = abs(entry_r - sl_r)
        if intent.notional_usd is None:
            size_coin, notional = self._size_position(entry_r, stop_distance)
        else:
            max_notional = self.bankroll * self.max_leverage
            notional = min(float(intent.notional_usd), max_notional)
            size_coin = notional / entry_r if entry_r > 0 else 0.0
        size_r = self._round_size(symbol, size_coin)
        if size_r <= 0:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=0.0,
                requested_leverage=0.0,
                entry_px=entry_r,
                tp_px=0.0,
                sl_px=sl_r,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False,
                status="rejected",
                error="position size rounds to zero",
            )

        notional = size_r * entry_r
        leverage = notional / self.bankroll if self.bankroll > 0 else 0.0
        stop_distance_usd = notional * (stop_distance / entry_r)
        verdict = self._risk.check_trade(
            symbol=symbol,
            side=side,
            size_usd=notional,
            leverage=leverage,
            stop_distance_usd=stop_distance_usd,
        )
        tp_r = self._round_to_tick(symbol, intent.tp_px) if intent.tp_px else 0.0
        if verdict != RiskVerdict.APPROVED:
            return OrderResult(
                signal_ts=intent.signal_ts,
                symbol=symbol,
                side=side,
                requested_size_usd=notional,
                requested_leverage=leverage,
                entry_px=entry_r,
                tp_px=tp_r,
                sl_px=sl_r,
                risk_verdict=verdict,
                filled=False,
                status="risk_rejected",
                size_coin=size_r,
                error=f"risk rejected: {verdict.value}",
            )

        order_requests = [
            {
                "name": symbol,
                "is_buy": is_buy,
                "sz": size_r,
                "limit_px": entry_r,
                "order_type": {"limit": {"tif": "Gtc"}},
                "reduce_only": False,
            },
        ]
        if tp_r > 0:
            order_requests.append(
                {
                    "name": symbol,
                    "is_buy": not is_buy,
                    "sz": size_r,
                    "limit_px": tp_r,
                    "order_type": {"trigger": {"triggerPx": tp_r, "isMarket": True, "tpsl": "tp"}},
                    "reduce_only": True,
                }
            )
        order_requests.append(
            {
                "name": symbol,
                "is_buy": not is_buy,
                "sz": size_r,
                "limit_px": sl_r,
                "order_type": {"trigger": {"triggerPx": sl_r, "isMarket": True, "tpsl": "sl"}},
                "reduce_only": True,
            }
        )
        return self._submit_bracket_orders(
            signal_ts=intent.signal_ts,
            symbol=symbol,
            side=side,
            notional=notional,
            leverage=leverage,
            entry_r=entry_r,
            tp_r=tp_r,
            sl_r=sl_r,
            size_r=size_r,
            verdict=verdict,
            order_requests=order_requests,
        )

    def _submit_bracket_orders(
        self,
        *,
        signal_ts: pd.Timestamp,
        symbol: str,
        side: str,
        notional: float,
        leverage: float,
        entry_r: float,
        tp_r: float,
        sl_r: float,
        size_r: float,
        verdict: RiskVerdict,
        order_requests: list[dict],
    ) -> OrderResult:
        """Submit bracket orders and normalize Hyperliquid response parsing."""
        expected_orders = len(order_requests)
        tp_index = 1 if expected_orders == 3 else None
        sl_index = expected_orders - 1
        try:
            resp = self.exchange.bulk_orders(order_requests, grouping=self.order_grouping)
        except Exception as e:
            logger.exception("bulk_orders failed")
            return OrderResult(
                signal_ts=signal_ts, symbol=symbol, side=side,
                requested_size_usd=notional, requested_leverage=leverage,
                entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
                risk_verdict=verdict, filled=False, status="exchange_error",
                size_coin=size_r, error=f"exchange error: {e}",
            )

        filled = False
        entry_oid = tp_oid = sl_oid = None
        error = None
        status = "unknown"
        needs_reconciliation = False
        cancel_attempted = False
        cancel_response = None
        entry_status = tp_status = sl_status = None
        try:
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            status_by_idx = {i: None for i in range(expected_orders)}
            for i, st in enumerate(statuses):
                if i in status_by_idx:
                    status_by_idx[i] = st
                if "error" in st:
                    error = f"order {i}: {st['error']}"
                elif "resting" in st:
                    oid = st["resting"]["oid"]
                    if i == 0:
                        entry_oid = oid
                    elif tp_index is not None and i == tp_index:
                        tp_oid = oid
                    elif i == sl_index:
                        sl_oid = oid
                elif "filled" in st:
                    oid = st["filled"].get("oid")
                    if i == 0:
                        entry_oid = oid
                    elif tp_index is not None and i == tp_index:
                        tp_oid = oid
                    elif i == sl_index:
                        sl_oid = oid
            entry_status = status_by_idx.get(0)
            tp_status = status_by_idx.get(tp_index) if tp_index is not None else None
            sl_status = status_by_idx.get(sl_index)
            entry_live = entry_oid is not None
            tp_ok = tp_index is None or tp_oid is not None
            sl_ok = sl_oid is not None
            child_failure = error is not None and entry_live
            if child_failure:
                cancel_attempted, cancel_response, cancel_error = self._cancel_entry_if_possible(symbol, entry_oid)
                needs_reconciliation = True
                if cancel_error:
                    error = f"{error}; orphan cancel failed: {cancel_error}"
                elif cancel_attempted:
                    error = f"{error}; attempted to cancel orphan entry oid={entry_oid}"
                status = "orphan_error"
            elif error:
                status = "rejected"
            elif entry_live and tp_ok and sl_ok:
                status = "submitted"
            else:
                error = f"incomplete order response: {statuses}"
                status = "reconcile_unknown"
                needs_reconciliation = True
            filled = status == "submitted"
        except Exception as e:
            error = f"parse error: {e}; raw={resp}"
            status = "parse_error"
            needs_reconciliation = True

        return OrderResult(
            signal_ts=signal_ts, symbol=symbol, side=side,
            requested_size_usd=notional, requested_leverage=leverage,
            entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
            risk_verdict=verdict, filled=filled, status=status,
            size_coin=size_r, needs_reconciliation=needs_reconciliation,
            cancel_attempted=cancel_attempted, error=error,
            entry_oid=entry_oid, tp_oid=tp_oid, sl_oid=sl_oid,
            entry_status=entry_status, tp_status=tp_status, sl_status=sl_status,
            cancel_response=cancel_response,
            response=resp,
        )

    def execute(self, signal: CascadeSignal, candles: pd.DataFrame,
                current_price: Optional[float] = None) -> OrderResult:
        symbol = signal.symbol
        # v1 allowlist guard. Refuse execution on any symbol that isn't
        # in V1_TRADE_SYMBOLS. Other symbols are research-only; their
        # data is collected but no orders are placed.
        if symbol not in V1_TRADE_SYMBOLS:
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side=(
                    "long" if signal.direction == SignalDirection.LONG else "short"
                ),
                requested_size_usd=0.0, requested_leverage=0.0,
                entry_px=0.0, tp_px=0.0, sl_px=0.0,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False, status="rejected_v1_allowlist",
                error=(f"{symbol} is not in v1 allowlist "
                       f"({sorted(V1_TRADE_SYMBOLS)}); research-only"),
            )
        if signal.direction == SignalDirection.NO_TRADE:
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side="no_trade",
                requested_size_usd=0.0, requested_leverage=0.0,
                entry_px=0.0, tp_px=0.0, sl_px=0.0,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False, status="rejected", error="signal direction is NO_TRADE",
            )

        is_buy = signal.direction == SignalDirection.LONG
        side_str = "long" if is_buy else "short"

        if current_price is None:
            if candles.empty:
                return OrderResult(
                    signal_ts=signal.timestamp, symbol=symbol, side=side_str,
                    requested_size_usd=0.0, requested_leverage=0.0,
                    entry_px=0.0, tp_px=0.0, sl_px=0.0,
                    risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                    filled=False, status="rejected",
                    error="current_price is required when candles are empty",
                )
            current_price = float(candles["close"].iloc[-1])
        atr = self._atr(candles)
        if atr <= 0:
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side=side_str,
                requested_size_usd=0.0, requested_leverage=0.0,
                entry_px=self._round_to_tick(symbol, current_price),
                tp_px=0.0, sl_px=0.0,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False, status="rejected",
                error="insufficient candle history for ATR; refusing to synthesize stop distance",
            )

        if is_buy:
            entry = current_price
            tp_px = current_price + atr * self.tp_atr_multiple
            sl_px = current_price - atr * self.sl_atr_multiple
            sl_distance = entry - sl_px
        else:
            entry = current_price
            tp_px = current_price - atr * self.tp_atr_multiple
            sl_px = current_price + atr * self.sl_atr_multiple
            sl_distance = sl_px - entry

        entry_r = self._round_to_tick(symbol, entry)
        tp_r = self._round_to_tick(symbol, tp_px)
        sl_r = self._round_to_tick(symbol, sl_px)

        size_coin, notional = self._size_position(entry_r, sl_distance)
        size_r = self._round_size(symbol, size_coin)
        if size_r <= 0:
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side=side_str,
                requested_size_usd=0.0, requested_leverage=0.0,
                entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
                risk_verdict=RiskVerdict.REJECTED_RISK_PCT,
                filled=False, status="rejected", error="position size rounds to zero",
            )

        notional = size_r * entry_r
        leverage = notional / self.bankroll if self.bankroll > 0 else 0.0
        # Risk check: stop_distance_usd is what we'd lose at the SL
        sl_distance_pct = sl_distance / entry_r if entry_r > 0 else 0
        stop_distance_usd = notional * sl_distance_pct
        verdict = self._risk.check_trade(
            symbol=symbol, side=side_str,
            size_usd=notional, leverage=leverage,
            stop_distance_usd=stop_distance_usd,
        )
        if verdict != RiskVerdict.APPROVED:
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side=side_str,
                requested_size_usd=notional, requested_leverage=leverage,
                entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
                risk_verdict=verdict, filled=False, status="risk_rejected",
                size_coin=size_r,
                error=f"risk rejected: {verdict.value}",
            )

        order_requests = [
            {
                "name": symbol,
                "is_buy": is_buy,
                "sz": size_r,
                "limit_px": entry_r,
                "order_type": {"limit": {"tif": "Gtc"}},
                "reduce_only": False,
            },
            {
                "name": symbol,
                "is_buy": not is_buy,
                "sz": size_r,
                "limit_px": tp_r,
                "order_type": {"trigger": {"triggerPx": tp_r, "isMarket": True, "tpsl": "tp"}},
                "reduce_only": True,
            },
            {
                "name": symbol,
                "is_buy": not is_buy,
                "sz": size_r,
                "limit_px": sl_r,
                "order_type": {"trigger": {"triggerPx": sl_r, "isMarket": True, "tpsl": "sl"}},
                "reduce_only": True,
            },
        ]
        try:
            resp = self.exchange.bulk_orders(order_requests, grouping=self.order_grouping)
        except Exception as e:
            logger.exception("bulk_orders failed")
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side=side_str,
                requested_size_usd=notional, requested_leverage=leverage,
                entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
                risk_verdict=verdict, filled=False, status="exchange_error",
                size_coin=size_r, error=f"exchange error: {e}",
            )

        filled = False
        entry_oid = tp_oid = sl_oid = None
        error = None
        status = "unknown"
        needs_reconciliation = False
        cancel_attempted = False
        cancel_response = None
        entry_status = tp_status = sl_status = None
        try:
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            status_by_idx = {0: None, 1: None, 2: None}
            for i, st in enumerate(statuses):
                if i in status_by_idx:
                    status_by_idx[i] = st
                if "error" in st:
                    error = f"order {i}: {st['error']}"
                elif "resting" in st:
                    oid = st["resting"]["oid"]
                    if i == 0:
                        entry_oid = oid
                    elif i == 1:
                        tp_oid = oid
                    elif i == 2:
                        sl_oid = oid
                elif "filled" in st:
                    oid = st["filled"].get("oid")
                    if i == 0:
                        entry_oid = oid
                    elif i == 1:
                        tp_oid = oid
                    elif i == 2:
                        sl_oid = oid
            entry_status = status_by_idx[0]
            tp_status = status_by_idx[1]
            sl_status = status_by_idx[2]
            entry_live = entry_oid is not None
            child_failure = error is not None and entry_live
            if child_failure:
                cancel_attempted, cancel_response, cancel_error = self._cancel_entry_if_possible(symbol, entry_oid)
                needs_reconciliation = True
                if cancel_error:
                    error = f"{error}; orphan cancel failed: {cancel_error}"
                elif cancel_attempted:
                    error = f"{error}; attempted to cancel orphan entry oid={entry_oid}"
                status = "orphan_error"
            elif error:
                status = "rejected"
            elif entry_live and tp_oid is not None and sl_oid is not None:
                status = "submitted"
            else:
                error = f"incomplete order response: {statuses}"
                status = "reconcile_unknown"
                needs_reconciliation = True
            filled = status == "submitted"
        except Exception as e:
            error = f"parse error: {e}; raw={resp}"
            status = "parse_error"
            needs_reconciliation = True

        return OrderResult(
            signal_ts=signal.timestamp, symbol=symbol, side=side_str,
            requested_size_usd=notional, requested_leverage=leverage,
            entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
            risk_verdict=verdict, filled=filled, status=status,
            size_coin=size_r, needs_reconciliation=needs_reconciliation,
            cancel_attempted=cancel_attempted, error=error,
            entry_oid=entry_oid, tp_oid=tp_oid, sl_oid=sl_oid,
            entry_status=entry_status, tp_status=tp_status, sl_status=sl_status,
            cancel_response=cancel_response,
            response=resp,
        )

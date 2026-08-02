"""
HyphyLiquid - Order manager.

Takes a CascadeSignal, validates against risk.py, sizes the position,
and places the entry + take-profit + stop-loss atomically via
bulk_orders with grouping="positionTpsl".

The atomic placement matters: if the entry fails, neither TP nor SL
exist. If entry fills, both TP and SL are live in the same block.

Usage:
    from src.execution.order_manager import OrderManager
    mgr = OrderManager.from_env()
    result = mgr.execute(signal, candles, current_price=...)
    # result.filled: bool
    # result.entry_oid, tp_oid, sl_oid
    # result.error if not filled
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

from src.risk import (
    RiskConfig,
    RiskManager,
    RiskState,
    RiskVerdict,
)
from src.strategy.cascade import CascadeSignal, SignalDirection

logger = logging.getLogger(__name__)


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
    error: Optional[str] = None
    entry_oid: Optional[int] = None
    tp_oid: Optional[int] = None
    sl_oid: Optional[int] = None
    response: Optional[dict] = None


class OrderManager:
    def __init__(self, exchange, info, bankroll: float,
                 risk_per_trade_pct: float = 0.01,
                 max_leverage: float = 10.0,
                 tp_atr_multiple: float = 2.0,
                 sl_atr_multiple: float = 1.0,
                 env: str = "testnet",
                 risk_state: Optional[RiskState] = None):
        self.exchange = exchange
        self.info = info
        self.bankroll = bankroll
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_leverage = max_leverage
        self.tp_atr_multiple = tp_atr_multiple
        self.sl_atr_multiple = sl_atr_multiple
        self.env = env
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
        ticks = {"BTC": 1.0, "ETH": 0.1, "SOL": 0.01}
        tick = ticks.get(symbol, 0.01)
        return round(round(price / tick) * tick, 6)

    def _round_size(self, symbol: str, size: float) -> float:
        decimals = {"BTC": 5, "ETH": 4, "SOL": 2}
        d = decimals.get(symbol, 3)
        return round(size, d)

    def execute(self, signal: CascadeSignal, candles: pd.DataFrame,
                current_price: Optional[float] = None) -> OrderResult:
        symbol = signal.symbol
        is_buy = signal.direction == SignalDirection.LONG
        side_str = "long" if is_buy else "short"

        if current_price is None:
            current_price = float(candles["close"].iloc[-1])
        atr = self._atr(candles)
        if atr <= 0:
            atr = current_price * 0.005

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
                filled=False, error="position size rounds to zero",
            )

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
                risk_verdict=verdict, filled=False,
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
            resp = self.exchange.bulk_orders(order_requests, grouping="positionTpsl")
        except Exception as e:
            logger.exception("bulk_orders failed")
            return OrderResult(
                signal_ts=signal.timestamp, symbol=symbol, side=side_str,
                requested_size_usd=notional, requested_leverage=leverage,
                entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
                risk_verdict=verdict, filled=False, error=f"exchange error: {e}",
            )

        filled = False
        entry_oid = tp_oid = sl_oid = None
        error = None
        try:
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            for i, st in enumerate(statuses):
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
            filled = entry_oid is not None and error is None
        except Exception as e:
            error = f"parse error: {e}; raw={resp}"

        return OrderResult(
            signal_ts=signal.timestamp, symbol=symbol, side=side_str,
            requested_size_usd=notional, requested_leverage=leverage,
            entry_px=entry_r, tp_px=tp_r, sl_px=sl_r,
            risk_verdict=verdict, filled=filled, error=error,
            entry_oid=entry_oid, tp_oid=tp_oid, sl_oid=sl_oid,
            response=resp,
        )

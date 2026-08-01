"""
HyphyLiquid — Hyperliquid SDK wrapper (market data layer)

Read-only methods for now. Authenticated order placement lives in
src/execution/order_manager.py (next module to build).

Public endpoints (no auth needed) covered here:
- meta + metaAndAssetCtxs (perp universe, live state)
- allMids (current mid prices)
- l2Book (orderbook)
- candlesSnapshot (historical OHLCV)
- fundingHistory (funding rate over time)

Usage:
    client = HyperliquidClient(env="testnet")
    df = client.get_candles("BTC", interval="1h", start_ms=..., end_ms=...)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from hyperliquid.info import Info
from hyperliquid.utils import constants

logger = logging.getLogger(__name__)

VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M"}


def _ms_now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ms_ago(days: int) -> int:
    return _ms_now() - days * 24 * 60 * 60 * 1000


class HyperliquidClient:
    """Thin wrapper around the official Hyperliquid SDK for market data."""

    def __init__(self, env: str = "testnet", skip_ws: bool = True):
        if env == "testnet":
            self.base_url = constants.TESTNET_API_URL
        elif env == "mainnet":
            self.base_url = constants.MAINNET_API_URL
        else:
            raise ValueError(f"env must be 'testnet' or 'mainnet', got {env!r}")
        self.env = env
        self.info = Info(self.base_url, skip_ws=skip_ws)
        logger.info("Hyperliquid client connected to %s (%s)", env, self.base_url)

    # ----- Universe & live state -----

    def get_meta(self) -> Dict[str, Any]:
        """Return the full perp universe metadata."""
        return self.info.meta()

    def get_meta_and_ctxs(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Return (meta, asset_contexts) — meta has universe, ctxs have live state."""
        data = self.info.meta_and_asset_ctxs()
        return data[0], data[1]

    def get_perp_summary(self) -> pd.DataFrame:
        """One-row-per-perp summary sorted by 24h volume (descending)."""
        meta, ctxs = self.get_meta_and_ctxs()
        universe = meta["universe"]
        rows = []
        for i, coin_meta in enumerate(universe):
            if i >= len(ctxs):
                continue
            ctx = ctxs[i]
            mark_px = float(ctx.get("markPx") or 0)
            rows.append(
                {
                    "symbol": coin_meta.get("name"),
                    "max_leverage": coin_meta.get("maxLeverage"),
                    "mark_px": mark_px,
                    "mid_px": float(ctx.get("midPx") or 0),
                    "oracle_px": float(ctx.get("oraclePx") or 0),
                    "open_interest_usd": float(ctx.get("openInterest") or 0) * mark_px,
                    "day_ntl_vol_usd": float(ctx.get("dayNtlVlm") or 0),
                    "funding_hourly": float(ctx.get("funding") or 0),
                    "premium": float(ctx.get("premium") or 0),
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.sort_values("day_ntl_vol_usd", ascending=False).reset_index(drop=True)

    def get_all_mids(self) -> Dict[str, float]:
        """Return current mid prices for all perps."""
        return {coin: float(px) for coin, px in self.info.all_mids().items()}

    def get_mid(self, coin: str) -> float:
        """Return the current mid price for a single coin."""
        return float(self.info.all_mids()[coin])

    # ----- Historical data -----

    def get_candles(
        self,
        coin: str,
        interval: str = "1h",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        lookback_days: Optional[int] = None,
    ) -> pd.DataFrame:
        """Fetch historical candles. Returns a DataFrame with columns:
        timestamp, open, high, low, close, volume.

        Args:
            coin: symbol like "BTC", "ETH"
            interval: "1m", "5m", "15m", "1h", "4h", "1d", etc.
            start_ms: start time in ms (UTC). Ignored if lookback_days given.
            end_ms: end time in ms (UTC). Defaults to now.
            lookback_days: convenience — set start_ms to (end_ms - N days).
        """
        if interval not in VALID_INTERVALS:
            raise ValueError(
                f"interval must be one of {sorted(VALID_INTERVALS)}, got {interval!r}"
            )

        if end_ms is None:
            end_ms = _ms_now()
        if lookback_days is not None:
            start_ms = _ms_ago(lookback_days)
        if start_ms is None:
            start_ms = _ms_ago(30)  # default 30 days

        raw = self.info.candles_snapshot(coin, interval, start_ms, end_ms)
        if not raw:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )

        df = pd.DataFrame(raw)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        df = df.rename(
            columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        )
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    def get_funding_history(
        self,
        coin: str,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        lookback_days: Optional[int] = None,
    ) -> pd.DataFrame:
        """Fetch funding rate history. Returns DataFrame with columns:
        timestamp, coin, funding_rate, premium.
        """
        if end_ms is None:
            end_ms = _ms_now()
        if lookback_days is not None:
            start_ms = _ms_ago(lookback_days)
        if start_ms is None:
            start_ms = _ms_ago(30)

        raw = self.info.funding_history(coin, start_ms, end_ms)
        if not raw:
            return pd.DataFrame(
                columns=["timestamp", "coin", "funding_rate", "premium"]
            )

        df = pd.DataFrame(raw)
        df["timestamp"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df = df.rename(columns={"fundingRate": "funding_rate"})
        df["funding_rate"] = df["funding_rate"].astype(float)
        df["premium"] = df["premium"].astype(float)
        df = df[["timestamp", "coin", "funding_rate", "premium"]]
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    # ----- Orderbook -----

    def get_orderbook(self, coin: str, depth: int = 20) -> Dict[str, Any]:
        """Return L2 orderbook snapshot. Levels truncated to `depth` per side."""
        book = self.info.l2_book(coin)
        levels = book.get("levels", [[], []])
        return {
            "coin": coin,
            "timestamp": datetime.now(timezone.utc),
            "bids": [
                {"px": float(l["px"]), "sz": float(l["sz"])}
                for l in levels[0][:depth]
            ],
            "asks": [
                {"px": float(l["px"]), "sz": float(l["sz"])}
                for l in levels[1][:depth]
            ],
        }

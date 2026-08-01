"""
Tests for src/exchange/hyperliquid.py — mocked SDK, no real API calls.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.exchange.hyperliquid import HyperliquidClient


@pytest.fixture
def mock_info():
    """Patch the Info class so no real SDK connection is made."""
    with patch("src.exchange.hyperliquid.Info") as mock_cls:
        info = MagicMock()
        mock_cls.return_value = info
        yield info


@pytest.fixture
def client(mock_info) -> HyperliquidClient:
    return HyperliquidClient(env="testnet")


class TestClientInit:
    def test_testnet_url(self, mock_info):
        from hyperliquid.utils import constants
        c = HyperliquidClient(env="testnet")
        assert c.base_url == constants.TESTNET_API_URL
        assert c.env == "testnet"

    def test_mainnet_url(self, mock_info):
        from hyperliquid.utils import constants
        c = HyperliquidClient(env="mainnet")
        assert c.base_url == constants.MAINNET_API_URL
        assert c.env == "mainnet"

    def test_invalid_env_raises(self, mock_info):
        with pytest.raises(ValueError):
            HyperliquidClient(env="staging")


class TestGetMeta:
    def test_returns_meta(self, client, mock_info):
        mock_info.meta.return_value = {
            "universe": [{"name": "BTC"}, {"name": "ETH"}]
        }
        meta = client.get_meta()
        assert len(meta["universe"]) == 2


class TestGetAllMids:
    def test_returns_dict_of_floats(self, client, mock_info):
        mock_info.all_mids.return_value = {
            "BTC": "63000.5",
            "ETH": "1800.2",
        }
        mids = client.get_all_mids()
        assert mids == {"BTC": 63000.5, "ETH": 1800.2}
        assert all(isinstance(v, float) for v in mids.values())

    def test_get_mid_single(self, client, mock_info):
        mock_info.all_mids.return_value = {"BTC": "63000.5", "ETH": "1800.2"}
        assert client.get_mid("BTC") == 63000.5


class TestGetCandles:
    def test_returns_dataframe(self, client, mock_info):
        mock_info.candles_snapshot.return_value = [
            {
                "t": 1700000000000,
                "o": "100",
                "h": "110",
                "l": "90",
                "c": "105",
                "v": "1000",
            },
            {
                "t": 1700003600000,
                "o": "105",
                "h": "115",
                "l": "100",
                "c": "110",
                "v": "1100",
            },
        ]
        df = client.get_candles(
            "BTC", interval="1h", start_ms=1700000000000, end_ms=1700003600000
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert list(df.columns) == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
        assert df["close"].iloc[0] == 105.0
        assert df["close"].iloc[1] == 110.0
        assert df["volume"].iloc[0] == 1000.0

    def test_empty_candles_returns_empty_df(self, client, mock_info):
        mock_info.candles_snapshot.return_value = []
        df = client.get_candles("BTC", interval="1h", start_ms=0, end_ms=1)
        assert df.empty
        assert list(df.columns) == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

    def test_invalid_interval_raises(self, client):
        with pytest.raises(ValueError):
            client.get_candles("BTC", interval="2x")

    def test_lookback_days_convenience(self, client, mock_info):
        mock_info.candles_snapshot.return_value = []
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        client.get_candles("BTC", interval="1h", lookback_days=7)
        call_args = mock_info.candles_snapshot.call_args
        coin, interval, start_ms, end_ms = call_args[0]
        assert coin == "BTC"
        assert interval == "1h"
        # 7 days = 604,800,000 ms
        delta = end_ms - start_ms
        assert 604_700_000 < delta < 604_900_000  # tolerate a few ms of clock drift


class TestGetFundingHistory:
    def test_returns_dataframe(self, client, mock_info):
        mock_info.funding_history.return_value = [
            {
                "time": 1700000000000,
                "coin": "BTC",
                "fundingRate": "0.0001",
                "premium": "0.00005",
            },
            {
                "time": 1700028800000,
                "coin": "BTC",
                "fundingRate": "-0.0002",
                "premium": "-0.0001",
            },
        ]
        df = client.get_funding_history("BTC", start_ms=1700000000000)
        assert len(df) == 2
        assert "funding_rate" in df.columns
        assert df["funding_rate"].iloc[0] == 0.0001
        assert df["funding_rate"].iloc[1] == -0.0002

    def test_empty_funding(self, client, mock_info):
        mock_info.funding_history.return_value = []
        df = client.get_funding_history("BTC", start_ms=0)
        assert df.empty


class TestGetOrderbook:
    def test_returns_bids_asks(self, client, mock_info):
        mock_info.l2_book.return_value = {
            "coin": "BTC",
            "levels": [
                [
                    {"px": "100", "sz": "1.5", "n": 2},
                    {"px": "99", "sz": "2.0", "n": 3},
                ],
                [
                    {"px": "101", "sz": "1.0", "n": 1},
                    {"px": "102", "sz": "3.0", "n": 4},
                ],
            ],
        }
        book = client.get_orderbook("BTC", depth=2)
        assert len(book["bids"]) == 2
        assert len(book["asks"]) == 2
        assert book["bids"][0]["px"] == 100.0
        assert book["asks"][1]["sz"] == 3.0

    def test_depth_truncates(self, client, mock_info):
        mock_info.l2_book.return_value = {
            "levels": [
                [{"px": str(i), "sz": "1.0"} for i in range(20)],
                [{"px": str(100 + i), "sz": "1.0"} for i in range(20)],
            ]
        }
        book = client.get_orderbook("BTC", depth=5)
        assert len(book["bids"]) == 5
        assert len(book["asks"]) == 5


class TestGetPerpSummary:
    def test_builds_dataframe(self, client, mock_info):
        mock_info.meta_and_asset_ctxs.return_value = [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 40},
                    {"name": "ETH", "maxLeverage": 25},
                ]
            },
            [
                {
                    "markPx": "60000",
                    "midPx": "60001",
                    "oraclePx": "60005",
                    "openInterest": "100",
                    "dayNtlVlm": "1000000",
                    "funding": "0.0001",
                    "premium": "0.00005",
                },
                {
                    "markPx": "3000",
                    "midPx": "3000.5",
                    "oraclePx": "3001",
                    "openInterest": "500",
                    "dayNtlVlm": "500000",
                    "funding": "0.0002",
                    "premium": "0.0001",
                },
            ],
        ]
        df = client.get_perp_summary()
        assert len(df) == 2
        assert "symbol" in df.columns
        assert "open_interest_usd" in df.columns
        assert "day_ntl_vol_usd" in df.columns
        # BTC OI = 100 PAXG * $60,000 = $6M
        btc = df[df["symbol"] == "BTC"].iloc[0]
        assert btc["open_interest_usd"] == 6_000_000
        assert btc["day_ntl_vol_usd"] == 1_000_000
        # Sorted by 24h vol desc — BTC > ETH, so BTC first
        assert df.iloc[0]["symbol"] == "BTC"

    def test_empty_universe(self, client, mock_info):
        mock_info.meta_and_asset_ctxs.return_value = [{"universe": []}, []]
        df = client.get_perp_summary()
        assert df.empty

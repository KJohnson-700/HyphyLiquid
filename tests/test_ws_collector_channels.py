"""Only collect channels something actually reads.

Audited 2026-08-25: bbo, activeAssetCtx and a duplicate raw ws_trades copy were
writing ~2.3 GB/day that no running process consumed. These tests pin the
enabled set to the channels with a live consumer, and pin the consumers so a
channel cannot be silently orphaned again.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import collect_ws_data as c  # noqa: E402


def test_orphaned_channels_are_off():
    assert "bbo" not in c.ENABLED_CHANNELS
    assert "activeAssetCtx" not in c.ENABLED_CHANNELS


def test_channels_with_live_consumers_stay_on():
    # trades -> data/trades -> liquidation_monitor
    # l2Book -> data/ws_l2book -> event_features (via liquidation_monitor)
    # candle -> data/ws_candle -> research backtests
    for ch in ("trades", "l2Book", "candle"):
        assert ch in c.ENABLED_CHANNELS


def test_raw_trades_duplicate_is_off():
    """data/trades already holds every trade; the raw copy doubled the cost."""
    assert c.WRITE_RAW_WS_TRADES is False


def test_liquidation_monitor_still_reads_the_dir_we_keep_writing():
    import liquidation_monitor as lm
    assert lm.TRADE_DIR.name == "trades", (
        "liq reads data/trades; disabling that write would starve it")


def test_event_features_still_reads_l2book():
    src = (REPO_ROOT / "src" / "strategy" / "event_features.py").read_text()
    assert "ws_l2book" in src, (
        "l2Book is kept enabled only because event_features consumes it")

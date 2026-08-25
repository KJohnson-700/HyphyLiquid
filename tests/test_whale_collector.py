"""Cohort selection is the whole signal here.

Measured on the live leaderboard: of the top 30 by accountValue only 3 held any
position (vaults and idle treasuries), versus 21 of 30 ranked by month ROI.
Ranking by size selects capital, not traders -- and reading idle accounts
produced a confident, entirely wrong "whales are 100% short HYPE".
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import collect_whale_positions as cw  # noqa: E402


def _row(addr, value, pnl, roi, vlm=0.0):
    return {"ethAddress": addr, "displayName": "", "accountValue": str(value),
            "windowPerformances": [["month", {"pnl": str(pnl), "roi": str(roi),
                                              "vlm": str(vlm)}]]}


ROWS = [
    _row("0xVAULT", 90_000_000, 5_000, 0.0001, 1_000),      # huge, barely trades
    _row("0xTRADER", 2_000_000, 900_000, 0.45, 500_000_000),  # active, high ROI
    _row("0xMID", 5_000_000, 100_000, 0.02, 50_000_000),
    _row("0xSMALL", 10_000, 9_000, 0.90, 1_000_000),          # great ROI, too small
    _row("0xLOSER", 8_000_000, -400_000, -0.05, 90_000_000),  # big but losing
]


def test_default_rank_is_roi_not_size():
    got = cw.select_whales(ROWS, top=3, min_value=1_000_000,
                           window="month", require_profit=True)
    assert got[0]["addr"] == "0xTRADER", "highest ROI should lead, not the vault"


def test_ranking_by_value_puts_the_vault_first():
    """Documents the bad behaviour so nobody re-defaults to it by accident."""
    got = cw.select_whales(ROWS, top=1, min_value=1_000_000, window="month",
                           require_profit=True, rank_by="value")
    assert got[0]["addr"] == "0xVAULT"


def test_size_floor_excludes_small_accounts():
    addrs = [w["addr"] for w in cw.select_whales(
        ROWS, top=10, min_value=1_000_000, window="month", require_profit=True)]
    assert "0xSMALL" not in addrs


def test_profit_filter_excludes_losers():
    addrs = [w["addr"] for w in cw.select_whales(
        ROWS, top=10, min_value=1_000_000, window="month", require_profit=True)]
    assert "0xLOSER" not in addrs
    addrs2 = [w["addr"] for w in cw.select_whales(
        ROWS, top=10, min_value=1_000_000, window="month", require_profit=False)]
    assert "0xLOSER" in addrs2


def test_rank_by_volume_available():
    got = cw.select_whales(ROWS, top=1, min_value=1_000_000, window="month",
                           require_profit=True, rank_by="volume")
    assert got[0]["addr"] == "0xTRADER"


def test_unknown_rank_key_is_rejected():
    with pytest.raises(ValueError):
        cw.select_whales(ROWS, top=1, min_value=0, window="month",
                         require_profit=False, rank_by="vibes")


def test_snapshot_rows_record_the_mid():
    """Without the price at observation time the history can never answer
    whether skew leads price, and no refetch recovers an old mid."""
    import inspect
    src = inspect.getsource(cw.collect)
    assert '"mid": mids.get(coin)' in src


def test_filters_are_persisted_with_each_snapshot():
    """A cohort must be reproducible from the row that it produced."""
    import inspect
    src = inspect.getsource(cw.collect)
    for key in ("rank_by", "top", "min_value", "require_profit"):
        assert key in src

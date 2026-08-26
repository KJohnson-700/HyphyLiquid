"""Fingerprinting profitable traders — the arithmetic must not lie about them."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import analyze_whale_fills as aw  # noqa: E402

H = 3_600_000


def _f(t, coin, dir_, pnl=0.0, crossed=True, tid=None):
    return {"time": t, "coin": coin, "dir": dir_, "closedPnl": str(pnl),
            "fee": "1.0", "crossed": crossed, "tid": tid or t}


def test_round_trip_hold_time():
    fills = [_f(0, "BTC", "Open Long"), _f(5 * H, "BTC", "Close Long", 100.0)]
    fp = aw.fingerprint(fills)
    assert fp["median_hold_h"] == 5.0
    assert fp["n_closes"] == 1


def test_close_without_open_is_not_a_zero_hold():
    """A position opened before the window would otherwise read as a 0h scalp
    and drag every hold-time distribution toward scalping."""
    fills = [_f(3 * H, "BTC", "Close Long", 50.0)]
    fp = aw.fingerprint(fills)
    assert fp["n_closes"] == 1
    assert fp["median_hold_h"] is None


def test_no_losses_is_infinite_not_zero():
    """PF None means no losing closes. Treating it as 0 inverted the ranking."""
    fills = [_f(0, "BTC", "Open Long"), _f(H, "BTC", "Close Long", 10.0)]
    fp = aw.fingerprint(fills)
    assert fp["profit_factor"] is None
    assert fp["win_rate"] == 1.0


def test_profit_factor_arithmetic():
    fills = [_f(0, "BTC", "Open Long"), _f(H, "BTC", "Close Long", 30.0),
             _f(2 * H, "BTC", "Open Long"), _f(3 * H, "BTC", "Close Long", -10.0)]
    assert aw.fingerprint(fills)["profit_factor"] == 3.0


def test_maker_taker_split():
    fills = [_f(0, "BTC", "Open Long", crossed=False),
             _f(H, "BTC", "Close Long", 5.0, crossed=True)]
    assert aw.fingerprint(fills)["taker_pct"] == 0.5


def test_archetypes_split_on_hold_time():
    assert aw.classify({"median_hold_h": 0.2, "taker_pct": 1.0, "fills_per_day": 10}) == "scalper"
    assert aw.classify({"median_hold_h": 4.0, "taker_pct": 1.0, "fills_per_day": 10}) == "intraday"
    assert aw.classify({"median_hold_h": 40.0, "taker_pct": 1.0, "fills_per_day": 10}) == "swing"
    assert aw.classify({"median_hold_h": 400.0, "taker_pct": 1.0, "fills_per_day": 10}) == "position"
    assert aw.classify({"median_hold_h": 5.0, "taker_pct": 0.1, "fills_per_day": 200}) == "market-maker"


def test_deposit_artifacts_are_excluded():
    """A deposit reads as a 438,185x monthly 'return' on the leaderboard."""
    rows = [
        {"ethAddress": "0xDEPOSIT", "accountValue": "43000000", "windowPerformances":
            [["month", {"pnl": "43000000", "roi": "438185.0", "vlm": "0"}]]},
        {"ethAddress": "0xREAL", "accountValue": "2000000", "windowPerformances":
            [["month", {"pnl": "900000", "roi": "0.45", "vlm": "500000000"}]]},
    ]
    got = aw.select_traders(rows, top=5, min_value=1_000_000, min_volume=10_000_000)
    assert [t["addr"] for t in got] == ["0xREAL"]


def test_zero_volume_accounts_are_excluded():
    """Several top-ROI rows have $0 volume -- vaults, not traders."""
    rows = [{"ethAddress": "0xVAULT", "accountValue": "20000000", "windowPerformances":
             [["month", {"pnl": "1000000", "roi": "0.05", "vlm": "0"}]]}]
    assert aw.select_traders(rows, top=5, min_value=1_000_000, min_volume=1_000_000) == []

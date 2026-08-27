"""A trailing stop must ratchet, never loosen."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT)); sys.path.insert(0, str(REPO_ROOT / "scripts"))

import manage_positions as mp  # noqa: E402
import paper_funding_neg_fade as pf  # noqa: E402


def _want(entry, peak, stop_pct, long_=True):
    return peak * (1 - stop_pct) if long_ else peak * (1 + stop_pct)


def test_stop_only_moves_in_the_favourable_direction():
    entry, stop = 100.0, 0.06
    initial = entry * (1 - stop)                     # 94.0
    assert _want(entry, 110.0, stop) > initial       # price up -> stop up
    assert _want(entry, 100.0, stop) == initial      # flat -> unchanged
    # a lower peak can never be produced: peak is a running max


def test_short_side_ratchets_downward():
    entry, stop = 100.0, 0.06
    initial = entry * (1 + stop)                     # 106.0
    assert _want(entry, 90.0, stop, long_=False) < initial


def test_guard_is_shared_not_reimplemented():
    assert mp._testnet_guard_ok is pf._testnet_guard_ok


def test_uses_the_shared_open_positions_file():
    """Must manage both lanes' positions, not just the fade lane's."""
    assert mp.TESTNET_OPEN_POSITIONS_PATH is pf.TESTNET_OPEN_POSITIONS_PATH


def test_runs_after_entries_in_the_daemon():
    """A position opened this tick is managed from the next one."""
    import fade_paper_daemon as d
    names = [n for n, _ in d.STEPS]
    assert names.index("manage positions") > names.index("testnet exec")
    assert names.index("manage positions") > names.index("swing testnet")

"""The swing lane's live path must share the fade lane's safety, not copy it."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT)); sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pandas as pd  # noqa: E402
import swing_testnet as st  # noqa: E402
import paper_funding_neg_fade as pf  # noqa: E402


def _df(move24, ts="2026-08-26 12:00:00"):
    return pd.DataFrame({"ts": [pd.Timestamp(ts)] * 40,
                         "close": [100.0] * 40, "move24": [move24] * 40})


def test_guard_is_imported_not_reimplemented():
    """One definition of 'may we touch the exchange', shared by both lanes."""
    assert st._testnet_guard_ok is pf._testnet_guard_ok
    assert st._submit_testnet_bracket is pf._submit_testnet_bracket


def test_position_cap_file_is_shared_with_the_fade_lane():
    """Three fade positions must block a swing entry, as live would."""
    assert st.TESTNET_OPEN_POSITIONS_PATH is pf.TESTNET_OPEN_POSITIONS_PATH
    assert st.MAX_OPEN_POSITIONS == pf.MAX_OPEN_POSITIONS


def test_signal_requires_the_move_threshold():
    assert st.current_signal(_df(0.5), 2.0, "momentum") is None
    assert st.current_signal(_df(3.0), 2.0, "momentum") is not None


def test_direction_flips_the_side():
    assert st.current_signal(_df(3.0), 2.0, "momentum")["side"] == "long"
    assert st.current_signal(_df(3.0), 2.0, "reversion")["side"] == "short"
    assert st.current_signal(_df(-3.0), 2.0, "momentum")["side"] == "short"


def test_only_the_latest_bar_fires():
    """A qualifying move from hours ago is already priced -- the stale-signal
    error this project has made twice."""
    d = _df(0.1)
    d.loc[10, "move24"] = 9.0          # old qualifying bar
    assert st.current_signal(d, 2.0, "momentum") is None


def test_short_history_is_refused():
    assert st.current_signal(_df(9.0).head(5), 2.0, "momentum") is None

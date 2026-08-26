"""The strategy must not run on a panel that failed its checks.

Running the sim on a bad panel manufactures numbers instead of surfacing the
fault -- which is how a broken lane reported "ok" for 11 hours, and how the
daemon's own jsonl builders silently re-corrupted the funding panel overnight.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fade_paper_daemon as fpd  # noqa: E402


def test_panel_steps_run_before_the_strategy():
    names = [n for n, _ in fpd.STEPS]
    assert names.index("panel health") < names.index("fade paper")
    assert names.index("venue funding") < names.index("panel health")
    assert names.index("venue candles") < names.index("panel health")


def test_panel_steps_are_blocking():
    for required in ("venue funding", "venue candles", "panel health"):
        assert required in fpd.BLOCKING_STEPS


def test_snapshot_derived_builders_are_not_in_the_loop():
    """asset_ctx.funding is the *upcoming* rate; deriving from it shifts the
    panel an hour early and reintroduces look-ahead."""
    cmds = " ".join(" ".join(c) for _, c in fpd.STEPS)
    assert "build_funding_panel.py" not in cmds
    assert "build_panels_from_duckdb.py" not in cmds


def test_venue_builders_are_in_the_loop():
    cmds = " ".join(" ".join(c) for _, c in fpd.STEPS)
    assert "build_funding_from_venue.py" in cmds
    assert "build_candles_from_venue.py" in cmds


def test_run_once_reports_abort_separately_from_failure_count():
    """An aborted tick must be distinguishable from a quiet market."""
    import inspect
    src = inspect.getsource(fpd.run_once)
    assert "return failures, True" in src, "abort must be signalled to the caller"
    assert src.rstrip().endswith("return failures, aborted")

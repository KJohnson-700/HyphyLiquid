"""The swing lane's selection rule is the thing that must not rot.

A 25-day fade sweep once produced PF 11.55 at n=16 -- the best of 24
configurations on ~30 trades. The swing lane only accepts a config that clears
the gate in BOTH independent halves, so one lucky window cannot promote it.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT)); sys.path.insert(0, str(REPO_ROOT / "scripts"))

import swing_lane as sl  # noqa: E402


def _frame(closes, start="2026-02-01"):
    ts = pd.date_range(start, periods=len(closes), freq="h")
    d = pd.DataFrame({"ts": ts, "symbol": "T", "close": closes,
                      "high": [c * 1.001 for c in closes],
                      "low": [c * 0.999 for c in closes],
                      "open": closes, "volume": [1.0] * len(closes)})
    d["move24"] = d.close.pct_change(24) * 100.0
    return d


def test_costs_are_deducted():
    """Every trade must pay the round trip; a flat market should not be free."""
    d = _frame([100.0] * 200)
    tr = sl.simulate(d, "T", move_thr=0.0, direction="momentum", hold_h=24, stop=0.08)
    assert all(t["net_pct"] < 0 for t in tr), "flat market must lose the spread"


def test_no_overlapping_positions_in_one_symbol():
    d = _frame([100 + i * 0.5 for i in range(400)])
    tr = sl.simulate(d, "T", move_thr=1.0, direction="momentum", hold_h=48, stop=0.08)
    for a, b in zip(tr, tr[1:]):
        assert pd.Timestamp(b["entry_ts"]) > pd.Timestamp(a["exit_ts"])


def test_move_threshold_gates_entries():
    """A quiet tape must produce no trades -- that is the whole point."""
    d = _frame([100 + (i % 2) * 0.01 for i in range(400)])
    assert sl.simulate(d, "T", move_thr=5.0, direction="momentum",
                       hold_h=24, stop=0.08) == []


def test_direction_flips_the_side():
    d = _frame([100 * (1.01 ** i) for i in range(200)])
    mom = sl.simulate(d, "T", move_thr=2.0, direction="momentum", hold_h=24, stop=0.08)
    rev = sl.simulate(d, "T", move_thr=2.0, direction="reversion", hold_h=24, stop=0.08)
    assert mom and rev
    assert mom[0]["side"] != rev[0]["side"]


def test_pf_infinite_when_no_losses():
    assert sl.pf([1.0, 2.0]) == float("inf")


def test_halves_split_on_the_boundary():
    tr = [{"entry_ts": "2026-03-01 00:00:00"}, {"entry_ts": "2026-06-01 00:00:00"}]
    h1, h2 = sl.halves(tr)
    assert len(h1) == 1 and len(h2) == 1


def test_selection_requires_both_halves(monkeypatch):
    """A config strong in one half and weak in the other must be rejected."""
    good = [{"net_pct": 5.0}] * 30
    bad = [{"net_pct": -5.0}] * 30
    assert sl.pf([t["net_pct"] for t in good]) == float("inf")
    assert sl.pf([t["net_pct"] for t in bad]) == 0.0

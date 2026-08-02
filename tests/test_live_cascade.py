"""Tests for the live cascade detector."""
import json
from pathlib import Path

import pytest

from src.strategy.live_cascade import LiveSignal, detect_live_cascade


def _make_snapshot(
    long_trapped: float = 0.0,
    short_trapped: float = 0.0,
    long_near: float = 0.0,
    short_near: float = 0.0,
    long_fuel: float = 0.0,
    short_fuel: float = 0.0,
    fresh_24h: float = 0.0,
    spot: float = 60000.0,
) -> dict:
    return {
        "spot_at_compute": spot,
        "trapped_pct": {
            "longs_underwater_5pct": long_trapped,
            "shorts_underwater_5pct": short_trapped,
        },
        "forced_liq_proximity": {
            "longs_near_liq_2pct": long_near,
            "shorts_near_liq_2pct": short_near,
        },
        "cascade_mass": {
            "long": {"within_2pct": long_fuel},
            "short": {"within_2pct": short_fuel},
        },
        "fresh_money": {"net_bias_24h": fresh_24h},
        "crowd_leverage": {"long_avg": 15.0, "short_avg": 15.0},
    }


def test_no_setup_when_nothing_set():
    state = detect_live_cascade("BTC", _make_snapshot())
    assert state.signal == LiveSignal.NO_SETUP
    assert state.confidence == 0.0


def test_short_setup_when_longs_trapped():
    state = detect_live_cascade(
        "BTC",
        _make_snapshot(
            long_trapped=0.45, long_near=0.05,
            short_fuel=10_000_000, fresh_24h=-0.20,
        ),
    )
    assert state.signal == LiveSignal.SHORT
    assert state.confidence > 0.0


def test_long_setup_when_shorts_trapped():
    state = detect_live_cascade(
        "ETH",
        _make_snapshot(
            short_trapped=0.40, short_near=0.04,
            long_fuel=8_000_000, fresh_24h=0.15,
        ),
    )
    assert state.signal == LiveSignal.LONG
    assert state.confidence > 0.0


def test_no_setup_when_fresh_money_agrees_with_trapped():
    # longs trapped, but fresh money is also long -> bail out, no cascade
    state = detect_live_cascade(
        "BTC",
        _make_snapshot(
            long_trapped=0.45, long_near=0.05,
            short_fuel=10_000_000, fresh_24h=0.20,  # same direction as trapped
        ),
    )
    assert state.signal == LiveSignal.NO_SETUP


def test_no_setup_when_no_cascade_fuel():
    state = detect_live_cascade(
        "BTC",
        _make_snapshot(
            long_trapped=0.45, long_near=0.05,
            short_fuel=1_000_000,  # too small
            fresh_24h=-0.20,
        ),
    )
    assert state.signal == LiveSignal.NO_SETUP


def test_no_setup_when_both_sides_trapped():
    # ambiguous - both sides set up
    state = detect_live_cascade(
        "BTC",
        _make_snapshot(
            long_trapped=0.45, long_near=0.05,
            short_trapped=0.45, short_near=0.05,
            long_fuel=10_000_000, short_fuel=10_000_000,
            fresh_24h=0.0,  # neutral
        ),
    )
    assert state.signal == LiveSignal.NO_SETUP


def test_current_price_propagates():
    snap = _make_snapshot(long_trapped=0.45, long_near=0.05, short_fuel=10_000_000, fresh_24h=-0.20)
    state = detect_live_cascade("BTC", snap, current_price=61500.0)
    assert state.current_price == 61500.0
    assert abs(state.cascade_distance_pct - 2.5) < 0.01


def test_real_snapshot_file_loads():
    """The BTC snapshot we saved earlier should be loadable and produce a sensible state."""
    snap_path = Path(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data\hyperperps\btc_heatmap.json")
    if not snap_path.exists():
        pytest.skip("No saved BTC snapshot")
    snap = json.loads(snap_path.read_text())
    state = detect_live_cascade("BTC", snap)
    # Just verify it runs and returns a valid enum value
    assert state.signal in (LiveSignal.NO_SETUP, LiveSignal.LONG, LiveSignal.SHORT)
    assert 0.0 <= state.confidence <= 1.0
    # Log for the human reading the test output
    print(f"\n  BTC live state: {state.signal.value}  conf={state.confidence:.2f}  reason={state.reason}")

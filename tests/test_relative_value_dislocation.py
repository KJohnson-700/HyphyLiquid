"""Unit tests for run_relative_value_dislocation.py.

All math is tested against hand-calculated expected values, not against
the live data. If the live run disagrees with these tests, the run is
wrong, not the test.

Run from repo root:
    python -m pytest tests/test_relative_value_dislocation.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_relative_value_dislocation import (  # noqa: E402
    BETA_WINDOW_MIN,
    CONFIRM_MIN_RETURN_PCT,
    FIXED_CALM_VOL_BTC_30M,
    FIXED_CALM_VOL_ETH_30M,
    FUNDING_NEUTRAL_BAND,
    OI_DIRECTION_THRESHOLD_PCT,
    PromotionVerdict,
    _beta,
    _calm_vol_threshold,
    _confirm_threshold_pct,
    _deviation_from_beta,
    _fade_pnl,
    _forward_return,
    _funding_bucket,
    _oi_direction_bucket,
    _realized_vol,
    _top_win_share,
    apply_promotion_gate,
    compute_per_event_records,
)


# ----------------------------- _forward_return ----------------------------- #


def test_forward_return_basic_up():
    # entry 100, exit 105 -> +5%
    candles = [
        {"t": 0, "c": 100.0},
        {"t": 60_000, "c": 105.0},
    ]
    pct = _forward_return(candles, entry_ts_ms=0, exit_ts_ms=60_000)
    assert pct == pytest.approx(5.0)


def test_forward_return_basic_down():
    candles = [
        {"t": 0, "c": 100.0},
        {"t": 60_000, "c": 95.0},
    ]
    pct = _forward_return(candles, entry_ts_ms=0, exit_ts_ms=60_000)
    assert pct == pytest.approx(-5.0)


def test_forward_return_finds_nearest_bar():
    # entry at t=10_000 (no exact bar) -> uses bar at t=0 (c=100)
    # exit at t=70_000 -> uses bar at t=60_000 (c=110)
    candles = [
        {"t": 0, "c": 100.0},
        {"t": 60_000, "c": 110.0},
        {"t": 120_000, "c": 120.0},
    ]
    pct = _forward_return(candles, entry_ts_ms=10_000, exit_ts_ms=70_000)
    assert pct == pytest.approx(10.0)


def test_forward_return_zero_entry_raises():
    candles = [{"t": 0, "c": 0.0}, {"t": 60_000, "c": 1.0}]
    with pytest.raises(ValueError):
        _forward_return(candles, 0, 60_000)


def test_forward_return_no_entry_bar_returns_none():
    candles = [{"t": 100_000, "c": 100.0}]
    # entry_ts is BEFORE all bars -> no eligible bar
    assert _forward_return(candles, 0, 200_000) is None


# ------------------------------- _fade_pnl --------------------------------- #


def test_fade_pnl_side_a_short_against_up_move():
    # side A (cascade up) -> fade is short -> fade_pnl = -raw
    assert _fade_pnl(side="A", raw_return_pct=+5.0) == pytest.approx(-5.0)


def test_fade_pnl_side_b_long_with_down_move():
    # side B (cascade down) -> fade is long -> fade_pnl = +raw
    assert _fade_pnl(side="B", raw_return_pct=-3.0) == pytest.approx(-3.0)


def test_fade_pnl_side_a_short_with_down_move_is_loss():
    # side A (cascade up); price actually fell -> short is loss
    assert _fade_pnl(side="A", raw_return_pct=-2.0) == pytest.approx(+2.0)


def test_fade_pnl_invalid_side_raises():
    with pytest.raises(ValueError):
        _fade_pnl(side="X", raw_return_pct=1.0)


# -------------------------------- _beta ------------------------------------ #


def test_beta_perfect_two_times():
    # alt = 2 * btc each minute, with some noise (none for unit test)
    btc = [0.001, -0.002, 0.003, -0.001, 0.002]
    alt = [2 * x for x in btc]
    assert _beta(btc, alt) == pytest.approx(2.0, abs=1e-9)


def test_beta_negative_correlation():
    btc = [0.001, -0.002, 0.003, -0.001, 0.002]
    alt = [-x for x in btc]
    assert _beta(btc, alt) == pytest.approx(-1.0, abs=1e-9)


def test_beta_zero_variance_returns_zero():
    btc = [0.0, 0.0, 0.0, 0.0]
    alt = [0.001, -0.002, 0.003, -0.001]
    # var(btc) = 0 -> beta undefined; contract: return 0.0 (sentinel)
    assert _beta(btc, alt) == 0.0


def test_beta_length_mismatch_raises():
    with pytest.raises(ValueError):
        _beta([0.1, 0.2], [0.1, 0.2, 0.3])


def test_beta_too_short_window_returns_none():
    # Need at least 2 points to compute covariance
    assert _beta([0.1], [0.1]) is None


# -------------------------- _realized_vol --------------------------------- #


def test_realized_vol_constant_returns_zero():
    rets = [0.001, 0.001, 0.001, 0.001, 0.001]
    assert _realized_vol(rets) == pytest.approx(0.0, abs=1e-12)


def test_realized_vol_known_sample():
    # hand-computed: mean=0, squared deviations sum=0.0001, var=0.00002, std=sqrt(0.00002)
    rets = [0.01, -0.01, 0.01, -0.01, 0.0]  # 5 points
    expected = math.sqrt(sum((r - 0.0) ** 2 for r in rets) / 5)
    assert _realized_vol(rets) == pytest.approx(expected, rel=1e-9)


def test_realized_vol_too_short_returns_none():
    assert _realized_vol([]) is None
    assert _realized_vol([0.001]) is None


# -------------------------- _deviation_from_beta -------------------------- #


def test_deviation_alt_moved_more_than_beta_expected():
    # expected: alt = beta * btc; actual alt moved 3x that -> deviation = 2*beta*btc
    btc_ret = 0.01  # +1%
    beta = 2.0
    alt_actual = 0.03  # alt actually moved +3%
    expected = beta * btc_ret  # 0.02
    deviation = alt_actual - expected  # 0.01
    assert _deviation_from_beta(alt_actual, beta, btc_ret) == pytest.approx(deviation)


def test_deviation_zero_when_alt_moves_as_expected():
    assert _deviation_from_beta(alt_actual=0.02, beta=2.0, ref_actual=0.01) == pytest.approx(0.0)


# ---------------------------- _funding_bucket ----------------------------- #


def test_funding_bucket_positive():
    assert _funding_bucket(0.0005) == "positive"  # 5 bps/hr


def test_funding_bucket_negative():
    assert _funding_bucket(-0.0005) == "negative"


def test_funding_bucket_neutral_within_band():
    assert _funding_bucket(0.00001) == "neutral"  # 0.1 bps/hr, inside band


def test_funding_bucket_none_input():
    assert _funding_bucket(None) == "unknown"


def test_funding_bucket_band_boundary():
    # exactly at the threshold -> still positive (strict greater)
    assert _funding_bucket(FUNDING_NEUTRAL_BAND) == "neutral"


# -------------------------- _oi_direction_bucket ------------------------- #


def test_oi_direction_up():
    # oi_now=110, oi_then=100 -> +10% -> up
    assert _oi_direction_bucket(oi_now=110.0, oi_then=100.0) == "up"


def test_oi_direction_down():
    assert _oi_direction_bucket(oi_now=90.0, oi_then=100.0) == "down"


def test_oi_direction_flat_small_change():
    # +0.5% -> inside flat band
    assert _oi_direction_bucket(oi_now=100.5, oi_then=100.0) == "flat"


def test_oi_direction_unknown_when_then_missing():
    assert _oi_direction_bucket(oi_now=100.0, oi_then=None) == "unknown"
    assert _oi_direction_bucket(oi_now=None, oi_then=100.0) == "unknown"


def test_oi_direction_zero_then_returns_unknown():
    assert _oi_direction_bucket(oi_now=100.0, oi_then=0.0) == "unknown"


# --------------------------- _top_win_share ------------------------------- #


def test_top_win_share_single_concentrated():
    # one big win + many small -> concentrated
    wins = [10.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    # total = 19.0, top = 10.0 -> share = 10/19 ~ 0.5263
    assert _top_win_share(wins) == pytest.approx(10.0 / 19.0)


def test_top_win_share_diversified():
    wins = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    assert _top_win_share(wins) == pytest.approx(0.1)


def test_top_win_share_empty_returns_zero():
    assert _top_win_share([]) == 0.0


def test_top_win_share_only_losses_returns_zero():
    # losses are filtered out; empty wins -> 0
    assert _top_win_share([-1.0, -2.0, -3.0]) == 0.0


# ---------------------- _confirm_threshold_pct ---------------------------- #


def test_confirm_threshold_default():
    assert _confirm_threshold_pct() == CONFIRM_MIN_RETURN_PCT


# ------------------------ compute_per_event_records ------------------------ #


def _make_candles(prices):
    """Build 1m candles starting at t=0, 1m apart, in fractional returns."""
    # prices are absolute; convert to bar records
    out = []
    for i, p in enumerate(prices):
        out.append({"t": i * 60_000, "o": p, "h": p, "l": p, "c": p, "v": 0.0, "n": 0})
    return out


def test_compute_per_event_records_smoke():
    # 200 minutes of flat candles for alt, BTC, ETH
    flat = [100.0] * 200
    candles = {
        "SOL": _make_candles(flat),
        "BTC": _make_candles(flat),
        "ETH": _make_candles(flat),
    }
    cascade = {
        "symbol": "SOL",
        "side": "B",
        "event_ts": "2026-08-04T00:00:00+00:00",
        "event_ts_ms": 90 * 60_000,  # 90m into data; 110m forward available
    }
    rec = compute_per_event_records(cascade, candles)
    assert rec is not None
    assert rec["symbol"] == "SOL"
    # flat market -> all forward returns ~0
    assert abs(rec["fade_pnl_30m"]) < 1e-9
    # confirm flags attached even without _attach_regime_flags
    assert rec["btc_confirms_alt_30m"] is False
    assert rec["eth_confirms_alt_30m"] is False


def test_compute_per_event_records_skips_outside_window():
    # 30m of candles; cascade event_ts is AFTER all of them -> no entry bar
    flat = [100.0] * 30
    candles = {
        "SOL": _make_candles(flat),
        "BTC": _make_candles(flat),
        "ETH": _make_candles(flat),
    }
    cascade = {
        "symbol": "SOL",
        "side": "B",
        "event_ts": "2026-08-04T00:00:00+00:00",
        "event_ts_ms": 200 * 60_000,  # way after last bar at 30*60_000
    }
    assert compute_per_event_records(cascade, candles) is None


def test_compute_per_event_records_btc_confirms_alt():
    # Build a synthetic situation: alt drops -2% in 30m, BTC drops -1% in 30m
    # Use 150 bars: 30 flat at 100, 30 where alt and btc decline, 90 flat at the new level
    # alt: 100 -> 98 over 30m -> -2%
    # btc: 100 -> 99 over 30m -> -1%
    n_bars = 150
    alt_prices = (
        [100.0] * 30
        + [100.0 - (i + 1) * (2.0 / 30) for i in range(30)]
        + [98.0] * (n_bars - 60)
    )
    btc_prices = (
        [100.0] * 30
        + [100.0 - (i + 1) * (1.0 / 30) for i in range(30)]
        + [99.0] * (n_bars - 60)
    )
    eth_prices = [100.0] * n_bars  # ETH flat
    candles = {
        "SOL": _make_candles(alt_prices),
        "BTC": _make_candles(btc_prices),
        "ETH": _make_candles(eth_prices),
    }
    # event at minute 30 (start of the move)
    cascade = {
        "symbol": "SOL",
        "side": "B",  # long liquidations -> price drops -> raw_return < 0
        "event_ts": "2026-08-04T00:30:00+00:00",
        "event_ts_ms": 30 * 60_000,
    }
    rec = compute_per_event_records(cascade, candles, horizons=(30,))
    assert rec is not None
    # btc moved down 1% in 30m, alt moved down 2% -> signs match
    # so btc confirms alt (bigger than 0.05% threshold)
    assert rec["btc_confirms_alt_30m"] is True
    # eth flat -> does NOT confirm
    assert rec["eth_confirms_alt_30m"] is False


# ------------------------- apply_promotion_gate --------------------------- #


def _ev(symbol, side, fade_pnl, n_seed=1):
    """Build a minimal event record for gate testing."""
    return {
        "symbol": symbol,
        "side": side,
        "event_ts": "2026-08-04T00:00:00+00:00",
        "fade_pnl_30m": fade_pnl,
        "isolated_30m": True,
        "btc_calm": True,
        "eth_calm": True,
        "btc_confirms_alt_30m": False,
        "eth_confirms_alt_30m": False,
    }


def test_promotion_gate_pass_all():
    # 40 events, all +0.5% -> n>=30, PF huge, med>0, top_win_share small
    events = [_ev("SOL", "B", 0.5) for _ in range(40)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.n == 40
    assert v.passed is True
    assert v.pf > 1.5
    assert v.median_pnl_pct > 0
    assert v.top_win_share <= 0.35


def test_promotion_gate_fail_low_n():
    events = [_ev("SOL", "B", 0.5) for _ in range(20)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.n == 20
    assert v.passed is False
    assert "n=" in v.reason


def test_promotion_gate_fail_low_pf():
    # 40 events, mix of big wins and big losses -> PF<1.5
    events = (
        [_ev("SOL", "B", 0.5) for _ in range(20)]
        + [_ev("SOL", "B", -1.0) for _ in range(20)]
    )
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.n == 40
    assert v.passed is False
    assert "PF=" in v.reason


def test_promotion_gate_fail_negative_median():
    # majority losses (median<0) but PF still > 1.5
    # 15 wins of +0.5% and 25 losses of -0.1%
    # gross_profit = 7.5, gross_loss = 2.5, PF = 3.0 > 1.5
    # median = -0.1% (because 25 of 40 are negative)
    events = (
        [_ev("SOL", "B", 0.5) for _ in range(15)]
        + [_ev("SOL", "B", -0.1) for _ in range(25)]
    )
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.n == 40
    assert v.pf > 1.5
    assert v.median_pnl_pct < 0
    assert v.passed is False
    assert "median=" in v.reason


def test_promotion_gate_fail_concentrated_wins():
    # 40 events, one huge win and many small losses
    events = (
        [_ev("SOL", "B", 0.01) for _ in range(39)]
        + [_ev("SOL", "B", 100.0)]  # one outsized winner
    )
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.n == 40
    # top win share = 100 / (100 + 39*0.01) = 100 / 100.39 ~ 0.996 -> > 0.35
    assert v.top_win_share > 0.35
    assert v.passed is False
    assert "top_win_share=" in v.reason


def test_promotion_gate_empty_bucket():
    v = apply_promotion_gate([], horizon_minutes=30)
    assert v.n == 0
    assert v.passed is False
    assert "n=0" in v.reason


# ---------------------- constants & module sanity ------------------------- #


def test_constants_are_sane():
    assert BETA_WINDOW_MIN > 0
    assert FIXED_CALM_VOL_BTC_30M > 0
    assert FIXED_CALM_VOL_ETH_30M > 0
    assert CONFIRM_MIN_RETURN_PCT > 0
    assert OI_DIRECTION_THRESHOLD_PCT > 0


def test_calm_vol_threshold_default_fixed():
    # Default: returns the fixed constants (5 / 8 bps per minute stdev)
    btc_thr, eth_thr = _calm_vol_threshold([])
    assert btc_thr == FIXED_CALM_VOL_BTC_30M
    assert eth_thr == FIXED_CALM_VOL_ETH_30M


def test_calm_vol_threshold_override():
    btc_thr, eth_thr = _calm_vol_threshold([], override_btc=0.002, override_eth=0.003)
    assert btc_thr == 0.002
    assert eth_thr == 0.003


def test_promotion_verdict_dataclass():
    # PromotionVerdict is a dataclass -> equality works
    a = PromotionVerdict(
        symbol="SOL", playbook="generic", horizon_minutes=30, n=10,
        win_rate=0.5, avg_pnl_pct=0.1, median_pnl_pct=0.05, pf=1.6,
        top_win_share=0.2, passed=True, reason="all gates met",
    )
    b = PromotionVerdict(
        symbol="SOL", playbook="generic", horizon_minutes=30, n=10,
        win_rate=0.5, avg_pnl_pct=0.1, median_pnl_pct=0.05, pf=1.6,
        top_win_share=0.2, passed=True, reason="all gates met",
    )
    assert a == b

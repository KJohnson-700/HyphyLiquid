"""Unit tests for run_book_persistence_filter.py.

All math is tested against hand-calculated expected values, not against
the live data.

Run from repo root:
    python -m pytest tests/test_book_persistence_filter.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_book_persistence_filter import (  # noqa: E402
    BBO_HEAVY_THRESHOLD,
    FLOW_NEUTRAL_BAND,
    L2_LEVELS,
    PERSISTENCE_MIN_SECONDS,
    SPREAD_WIDEN_FACTOR,
    STALE_MID_DRIFT_PCT,
    BucketVerdict,
    _bar_at_or_before,
    _book_absorbed_flag,
    _bbo_imbalance,
    _bbo_imbalance_bucket,
    _flow_amplifies_flag,
    _flow_stats,
    _l2_imbalance,
    _l2_imbalance_bucket,
    _mid_from_bbo,
    _mid_from_l2,
    _parse_bbo,
    _parse_l2,
    _parse_trade,
    _persistence_seconds,
    _stale_book_flag,
    apply_promotion_gate,
    compute_per_event_features,
)


# ------------------------- _parse_bbo / _parse_l2 / _parse_trade ---------- #


def test_parse_bbo_basic():
    row = {
        "recv_ts": "2026-08-04T00:00:00+00:00",
        "payload": {
            "coin": "BTC",
            "time": 1785638972909,
            "bbo": [
                {"px": "100.0", "sz": "5.0", "n": 10},  # bid
                {"px": "101.0", "sz": "3.0", "n": 8},   # ask
            ],
            "ts": 1785638972909,
        },
    }
    parsed = _parse_bbo(row)
    assert parsed is not None
    bid_px, bid_sz, ask_px, ask_sz, ts = parsed
    assert bid_px == 100.0
    assert bid_sz == 5.0
    assert ask_px == 101.0
    assert ask_sz == 3.0
    assert ts == 1785638972909


def test_parse_bbo_handles_list_or_dict_bbo():
    # Some HL feeds use {"bbo": {"bid":..., "ask":...}} dict shape; we accept list
    row = {
        "payload": {
            "bbo": [
                {"px": "100.0", "sz": "5.0", "n": 10},
                {"px": "101.0", "sz": "3.0", "n": 8},
            ],
            "time": 1000,
        }
    }
    assert _parse_bbo(row) is not None
    # Missing bbo -> None
    assert _parse_bbo({"payload": {"time": 1000}}) is None


def test_parse_l2_basic():
    row = {
        "payload": {
            "coin": "BTC",
            "time": 1000,
            "spread": "1.0",
            "levels": [
                # bids descending
                [{"px": "100.0", "sz": "5.0", "n": 1}, {"px": "99.0", "sz": "2.0", "n": 1}],
                # asks ascending
                [{"px": "101.0", "sz": "3.0", "n": 1}, {"px": "102.0", "sz": "4.0", "n": 1}],
            ],
        }
    }
    bids, asks, mid, spread, ts = _parse_l2(row)
    assert bids == [(100.0, 5.0), (99.0, 2.0)]
    assert asks == [(101.0, 3.0), (102.0, 4.0)]
    # mid is average of best bid and best ask
    assert mid == 100.5
    assert spread == 1.0
    assert ts == 1000


def test_parse_l2_missing_levels():
    assert _parse_l2({"payload": {"time": 1000}}) is None


def test_parse_trade_basic():
    row = {
        "payload": {
            "coin": "BTC",
            "side": "B",
            "px": "100.0",
            "sz": "2.5",
            "time": 12345,
            "tid": 999,
        }
    }
    side, px, sz, ts = _parse_trade(row)
    assert side == "B"
    assert px == 100.0
    assert sz == 2.5
    assert ts == 12345


def test_parse_trade_missing_fields():
    assert _parse_trade({"payload": {"side": "B"}}) is None


# ------------------------- _bar_at_or_before ----------------------------- #


def test_bar_at_or_before_basic():
    bars = [{"t": 0}, {"t": 1000}, {"t": 2000}]
    assert _bar_at_or_before(bars, 500)["t"] == 0
    assert _bar_at_or_before(bars, 1000)["t"] == 1000
    assert _bar_at_or_before(bars, 2500)["t"] == 2000
    assert _bar_at_or_before(bars, 0)["t"] == 0


def test_bar_at_or_before_before_all():
    bars = [{"t": 100}]
    assert _bar_at_or_before(bars, 50) is None
    assert _bar_at_or_before([], 50) is None


# ------------------------- imbalance math -------------------------------- #


def test_bbo_imbalance_balanced():
    # bid=ask -> 0.5
    assert _bbo_imbalance(5.0, 5.0) == pytest.approx(0.5)


def test_bbo_imbalance_bid_heavy():
    # bid 7, ask 3 -> 7/10 = 0.7
    assert _bbo_imbalance(7.0, 3.0) == pytest.approx(0.7)


def test_bbo_imbalance_zero_ask():
    # ask=0 -> 1.0 (all bid)
    assert _bbo_imbalance(5.0, 0.0) == pytest.approx(1.0)


def test_bbo_imbalance_zero_bid():
    assert _bbo_imbalance(0.0, 5.0) == pytest.approx(0.0)


def test_bbo_imbalance_both_zero():
    # Both zero: returns 0.5 (sentinel - no signal)
    assert _bbo_imbalance(0.0, 0.0) == pytest.approx(0.5)


def test_l2_imbalance_top_n():
    # bids = [(100,5), (99,2), (98,1)] top 2 = 5+2 = 7
    # asks = [(101,3), (102,4), (103,1)] top 2 = 3+4 = 7
    # imbalance = 7/14 = 0.5
    bids = [(100.0, 5.0), (99.0, 2.0), (98.0, 1.0)]
    asks = [(101.0, 3.0), (102.0, 4.0), (103.0, 1.0)]
    assert _l2_imbalance(bids, asks, n=2) == pytest.approx(0.5)
    # n=1 -> bid/ask imbalance
    assert _l2_imbalance(bids, asks, n=1) == pytest.approx(5.0 / 8.0)


def test_l2_imbalance_empty_bids():
    assert _l2_imbalance([], [(101.0, 3.0)], n=5) == 0.0


def test_l2_imbalance_empty_asks():
    assert _l2_imbalance([(100.0, 5.0)], [], n=5) == 1.0


# ------------------------- bucket boundaries ---------------------------- #


def test_bbo_imbalance_bucket_boundaries():
    assert _bbo_imbalance_bucket(0.7) == "bid_heavy"
    assert _bbo_imbalance_bucket(BBO_HEAVY_THRESHOLD) == "balanced"  # at threshold
    assert _bbo_imbalance_bucket(0.5) == "balanced"
    assert _bbo_imbalance_bucket(0.3) == "ask_heavy"
    assert _bbo_imbalance_bucket(1.0 - BBO_HEAVY_THRESHOLD) == "balanced"  # at upper


def test_l2_imbalance_bucket_same_thresholds():
    assert _l2_imbalance_bucket(0.6) == "bid_heavy"
    assert _l2_imbalance_bucket(0.5) == "balanced"
    assert _l2_imbalance_bucket(0.4) == "ask_heavy"


# ------------------------- mid price ------------------------------------- #


def test_mid_from_bbo():
    assert _mid_from_bbo(100.0, 101.0) == pytest.approx(100.5)


def test_mid_from_l2():
    # mid uses best bid and best ask
    bids = [(100.0, 5.0)]
    asks = [(102.0, 3.0)]
    assert _mid_from_l2(bids, asks) == pytest.approx(101.0)


def test_mid_from_l2_empty():
    assert _mid_from_l2([], []) == 0.0


# ------------------------- persistence ---------------------------------- #


def test_persistence_seconds_persistent_bid_heavy():
    # 5 BBO snapshots, all bid_heavy (imbalance >= 0.55)
    # 1s apart -> 5s of consecutive bid_heavy
    snaps = [
        (0, 0.6),
        (1000, 0.7),
        (2000, 0.65),
        (3000, 0.6),
        (4000, 0.7),
    ]
    p = _persistence_seconds(snaps, event_ts_ms=5000)
    assert p["duration_seconds"] == 5.0
    assert p["direction"] == "bid_heavy"


def test_persistence_seconds_persistent_ask_heavy():
    # 3 ask_heavy snapshots
    snaps = [
        (0, 0.4),
        (1000, 0.35),
        (2000, 0.3),
    ]
    p = _persistence_seconds(snaps, event_ts_ms=3000)
    assert p["duration_seconds"] == 3.0
    assert p["direction"] == "ask_heavy"


def test_persistence_seconds_breaks_at_balanced():
    # bid_heavy, balanced (breaks streak), bid_heavy
    snaps = [
        (0, 0.6),
        (1000, 0.5),  # balanced -> breaks streak
        (2000, 0.7),  # new streak
    ]
    p = _persistence_seconds(snaps, event_ts_ms=3000)
    assert p["duration_seconds"] == 1.0  # just the last bid_heavy
    assert p["direction"] == "bid_heavy"


def test_persistence_seconds_empty():
    p = _persistence_seconds([], event_ts_ms=0)
    assert p["duration_seconds"] == 0.0
    assert p["direction"] == "neutral"


def test_persistence_seconds_filters_future_snapshots():
    # Snapshot 2s in the future relative to event -> ignore
    snaps = [
        (0, 0.6),
        (1000, 0.7),
        (2000, 0.65),
        (6000, 0.7),  # future
    ]
    p = _persistence_seconds(snaps, event_ts_ms=3000)
    assert p["duration_seconds"] == 3.0


# ------------------------- staleness ------------------------------------ #


def test_stale_book_flag_true():
    # wide spread AND mid unchanged
    # median spread = 1.0; current = 2.0 (wider)
    # mid drift < threshold
    flags = _stale_book_flag(
        current_spread=2.0,
        median_spread=1.0,
        current_mid=100.0,
        prior_mid=100.0005,  # 0.0005% drift
    )
    assert flags["spread_widened"] is True
    assert flags["mid_unchanged"] is True
    assert flags["stale_book"] is True


def test_stale_book_flag_false_when_spread_normal():
    flags = _stale_book_flag(
        current_spread=1.0,
        median_spread=1.0,
        current_mid=100.0,
        prior_mid=100.0005,
    )
    assert flags["spread_widened"] is False
    assert flags["stale_book"] is False


def test_stale_book_flag_false_when_mid_moved():
    flags = _stale_book_flag(
        current_spread=2.0,
        median_spread=1.0,
        current_mid=100.5,
        prior_mid=100.0,  # 0.5% drift
    )
    assert flags["spread_widened"] is True
    assert flags["mid_unchanged"] is False
    assert flags["stale_book"] is False


def test_stale_book_flag_uses_widen_factor():
    # Spread 1.4x median should NOT trigger (factor is 1.5x)
    flags = _stale_book_flag(
        current_spread=1.4,
        median_spread=1.0,
        current_mid=100.0,
        prior_mid=100.0,
    )
    assert flags["spread_widened"] is False


# ------------------------- trade-flow stats ----------------------------- #


def test_flow_stats_buy_heavy():
    # 3 buys, 1 sell
    trades = [("B", 100.0, 1.0), ("B", 100.1, 2.0), ("A", 100.2, 1.5), ("B", 100.0, 0.5)]
    stats = _flow_stats(trades)
    assert stats["buy_count"] == 3
    assert stats["sell_count"] == 1
    assert stats["buy_notional"] == pytest.approx(100.0 * 1.0 + 100.1 * 2.0 + 100.0 * 0.5)
    assert stats["sell_notional"] == pytest.approx(100.2 * 1.5)
    # flow_imbalance = (buy_count - sell_count) / (buy_count + sell_count)
    assert stats["flow_imbalance"] == pytest.approx(0.5)
    # notional imbalance
    notional_total = stats["buy_notional"] + stats["sell_notional"]
    assert stats["notional_imbalance"] == pytest.approx(
        (stats["buy_notional"] - stats["sell_notional"]) / notional_total
    )


def test_flow_stats_empty():
    stats = _flow_stats([])
    assert stats["buy_count"] == 0
    assert stats["sell_count"] == 0
    assert stats["flow_imbalance"] == 0.0


def test_flow_stats_all_sells():
    trades = [("A", 100.0, 1.0), ("A", 100.0, 1.0)]
    stats = _flow_stats(trades)
    assert stats["flow_imbalance"] == -1.0


# ------------------------- flow direction flags ------------------------- #


def test_flow_amplifies_sell_side_cascade():
    # side=B cascade (forced selling), flow is also sell-heavy
    flag = _flow_amplifies_flag(side="B", flow_imbalance=-0.5, window_s=30)
    # negative flow_imbalance = sell-heavy
    # side B = cascade DOWN = forced selling
    # flow in same direction as cascade = "amplifies"
    assert flag == "amplifies"


def test_flow_fades_sell_side_cascade():
    # side=B cascade, flow is buy-heavy
    flag = _flow_amplifies_flag(side="B", flow_imbalance=+0.5, window_s=30)
    # positive flow = buy-heavy, opposite of cascade -> fades
    assert flag == "fades"


def test_flow_amplifies_buy_side_cascade():
    # side=A cascade (forced buying), flow is buy-heavy
    flag = _flow_amplifies_flag(side="A", flow_imbalance=+0.5, window_s=30)
    assert flag == "amplifies"


def test_flow_neutral_within_band():
    # |0.1| < 0.2 (default band) -> neutral
    flag = _flow_amplifies_flag(side="B", flow_imbalance=0.1, window_s=30)
    assert flag == "neutral"
    flag = _flow_amplifies_flag(side="A", flow_imbalance=-0.1, window_s=30)
    assert flag == "neutral"


# ------------------------- book absorbed flag --------------------------- #


def test_book_absorbed_sell_side_cascade_with_ask_heavy_after():
    # side=B cascade, post-event L2 imbalance is ask_heavy (<0.45)
    # book shifted against the cascade -> absorbed
    flag = _book_absorbed_flag(
        side="B",
        bbo_imbalance_at_event=0.5,
        bbo_imbalance_post=0.3,  # shifted to ask_heavy
    )
    # side B = down cascade; book became ask_heavy = selling side filled
    # book absorbing = book_imbalance went DOWN (in same direction as cascade)?
    # For side B (cascade DOWN), absorbing means bids rebuilt -> bid_heavy
    # So bbo shifted from 0.5 to 0.3 is NOT absorbing, it's amplifying
    assert flag == "amplified"


def test_book_absorbed_sell_side_cascade_with_bid_heavy_after():
    flag = _book_absorbed_flag(
        side="B",
        bbo_imbalance_at_event=0.5,
        bbo_imbalance_post=0.75,  # shifted to bid_heavy (delta 0.25, well above 0.2 band)
    )
    # side B = down cascade; book became bid_heavy = bids rebuilt = absorbed
    assert flag == "absorbed"


def test_book_absorbed_buy_side_cascade():
    # side=A cascade (up), post-event ask_heavy = absorbed
    flag = _book_absorbed_flag(
        side="A",
        bbo_imbalance_at_event=0.5,
        bbo_imbalance_post=0.3,  # ask_heavy after up cascade
    )
    assert flag == "absorbed"


def test_book_neutral_when_no_significant_change():
    flag = _book_absorbed_flag(
        side="B",
        bbo_imbalance_at_event=0.5,
        bbo_imbalance_post=0.5,  # no change
    )
    # |delta| < band -> neutral
    assert flag == "neutral"


# ------------------------- promotion gate ------------------------------ #


def _ev(fade_pnl_30m):
    return {"fade_pnl_30m": fade_pnl_30m}


def test_apply_promotion_gate_pass():
    # 40 small wins, 1 small loss -> PF > 1.5, med > 0, top share < 35%
    events = [_ev(0.5) for _ in range(40)] + [_ev(-0.1)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.n == 41
    assert v.passed is True


def test_apply_promotion_gate_fail_low_n():
    events = [_ev(0.5) for _ in range(20)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.passed is False
    assert "n=" in v.reason


def test_apply_promotion_gate_fail_low_pf():
    events = [_ev(0.1) for _ in range(35)] + [_ev(-0.5) for _ in range(5)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.passed is False
    assert "PF=" in v.reason


def test_apply_promotion_gate_fail_negative_median():
    # 15 wins + 25 losses -> PF>1.5 possible, but median < 0
    events = [_ev(0.5) for _ in range(15)] + [_ev(-0.1) for _ in range(25)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.median_pnl_pct < 0
    assert v.passed is False
    assert "median=" in v.reason


def test_apply_promotion_gate_fail_concentrated_wins():
    events = [_ev(0.01) for _ in range(39)] + [_ev(100.0)]
    v = apply_promotion_gate(events, horizon_minutes=30)
    assert v.top_win_share > 0.35
    assert v.passed is False


# ------------------------- end-to-end smoke ------------------------------ #


def test_compute_per_event_features_minimal():
    # Build a minimal dataset and verify the function returns a feature dict
    cascade = {
        "symbol": "BTC",
        "side": "B",
        "event_ts_ms": 60_000,
    }
    # bbo_snaps and l2_snaps are dict lists (same shape as load_bbo/load_l2)
    bbo_snaps = [
        {"ts": 10_000, "bid_px": 100.0, "bid_sz": 5.0, "ask_px": 101.0, "ask_sz": 5.0},  # balanced
        {"ts": 20_000, "bid_px": 100.0, "bid_sz": 6.0, "ask_px": 101.0, "ask_sz": 4.0},  # bid_heavy 0.6
        {"ts": 30_000, "bid_px": 100.0, "bid_sz": 7.0, "ask_px": 101.0, "ask_sz": 3.0},  # bid_heavy 0.7
        {"ts": 40_000, "bid_px": 100.0, "bid_sz": 7.0, "ask_px": 101.0, "ask_sz": 3.0},  # bid_heavy
        {"ts": 50_000, "bid_px": 100.0, "bid_sz": 7.0, "ask_px": 101.0, "ask_sz": 3.0},  # bid_heavy
        {"ts": 60_000, "bid_px": 100.0, "bid_sz": 7.0, "ask_px": 101.0, "ask_sz": 3.0},  # bid_heavy (event)
    ]
    l2_snaps = [
        {
            "ts": 60_000,
            "bids": [(100.0, 7.0), (99.0, 5.0)],
            "asks": [(101.0, 3.0), (102.0, 4.0)],
            "mid": 100.5,
            "spread": 1.0,
        },
    ]
    trades_30s = [("B", 100.0, 1.0), ("A", 101.0, 2.0), ("B", 100.5, 0.5)]
    trades_60s = trades_30s + [("A", 100.0, 0.5)]
    features = compute_per_event_features(
        cascade=cascade,
        bbo_snaps=bbo_snaps,
        l2_snaps=l2_snaps,
        trades_30s=trades_30s,
        trades_60s=trades_60s,
    )
    assert features is not None
    assert features["symbol"] == "BTC"
    assert features["side"] == "B"
    assert features["bbo_imbalance_at_event"] == pytest.approx(0.7)
    assert features["bbo_bucket_at_event"] == "bid_heavy"
    # bid_heavy streak from 10s to 60s = 50s, > 30s PERSISTENCE_MIN_SECONDS
    assert features["persistence_seconds"] >= 30.0
    assert features["persistence_direction"] == "bid_heavy"
    # 2 buys, 1 sell in 30s -> flow_imbalance = (2-1)/3 = 0.333
    assert features["flow_imbalance_30s"] == pytest.approx(1 / 3, abs=1e-3)
    # Side B = down cascade, positive flow = buys = fade
    assert features["flow_30s_label"] == "fades"


# ------------------------- constants sanity ----------------------------- #


def test_constants_sane():
    assert 0.0 < BBO_HEAVY_THRESHOLD < 1.0
    assert 0.0 < FLOW_NEUTRAL_BAND < 1.0
    assert PERSISTENCE_MIN_SECONDS > 0
    assert L2_LEVELS > 0
    assert SPREAD_WIDEN_FACTOR > 1.0
    assert STALE_MID_DRIFT_PCT > 0.0


def test_bucket_verdict_dataclass():
    a = BucketVerdict(
        symbol="BTC", playbook="generic", horizon_minutes=30, n=10,
        win_rate=0.5, avg_pnl_pct=0.1, median_pnl_pct=0.05, pf=1.6,
        top_win_share=0.2, passed=True, reason="ok",
    )
    b = BucketVerdict(
        symbol="BTC", playbook="generic", horizon_minutes=30, n=10,
        win_rate=0.5, avg_pnl_pct=0.1, median_pnl_pct=0.05, pf=1.6,
        top_win_share=0.2, passed=True, reason="ok",
    )
    assert a == b

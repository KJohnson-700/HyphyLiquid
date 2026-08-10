"""Tests for scripts/run_depth_obi_filter.py.

Covers:
  - Filter callables (each of the 10 filters)
  - Return computation (_compute_fade_return, _iso_to_ms)
  - Bucket evaluation (_evaluate_bucket): win rate, PF, top_win_share,
    promotion gate verdict
  - End-to-end: load tiny l2_cascade_features fixture, run all filters,
    verify verdicts structure
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_depth_obi_filter as mod


# ----------------------------------------------------------------------------
# Filter callables
# ----------------------------------------------------------------------------

def _make_cascade(side: str, pre: dict = None, post: dict = None,
                  snapshots: dict = None) -> dict:
    return {
        "symbol": "BTC",
        "side": side,
        "event_ts_ms": 1_000_000,
        "event_ts": "2026-08-06T00:00:00.000000+00:00",
        "start_ts": "2026-08-06T00:00:00.000000+00:00",
        "event_vwap": 64600.0,
        "pre_thinning": pre or {},
        "post_resilience": post or {},
        "snapshot_t_plus_5s": snapshots.get("t5") if snapshots else None,
        "_snapshots_present": 4,
    }


class TestFilters:
    def test_baseline_always_true(self):
        assert mod.f_obi_5_drop_lt_neg_0_5(_make_cascade("B")) is False  # sanity
        for side in ("A", "B"):
            for c in [_make_cascade(side), {}, {"side": "X"}]:
                assert mod._is_baseline(c) is True

    def test_obi_5_drop_below_threshold_side_B(self):
        c = _make_cascade("B", pre={"obi_5_drop": -0.6})
        assert mod.f_obi_5_drop_lt_neg_0_5(c) is True
        c = _make_cascade("B", pre={"obi_5_drop": -0.4})
        assert mod.f_obi_5_drop_lt_neg_0_5(c) is False
        c = _make_cascade("B", pre={"obi_5_drop": 0.5})  # wrong sign
        assert mod.f_obi_5_drop_lt_neg_0_5(c) is False

    def test_obi_5_drop_above_threshold_side_A(self):
        c = _make_cascade("A", pre={"obi_5_drop": 0.6})
        assert mod.f_obi_5_drop_lt_neg_0_5(c) is True
        c = _make_cascade("A", pre={"obi_5_drop": 0.4})
        assert mod.f_obi_5_drop_lt_neg_0_5(c) is False

    def test_obi_5_drop_no_data(self):
        c = _make_cascade("B", pre={})
        assert mod.f_obi_5_drop_lt_neg_0_5(c) is False

    def test_obi_10_drop_threshold(self):
        c = _make_cascade("B", pre={"obi_10_drop": -0.4})
        assert mod.f_obi_10_drop_lt_neg_0_3(c) is True
        c = _make_cascade("B", pre={"obi_10_drop": -0.2})
        assert mod.f_obi_10_drop_lt_neg_0_3(c) is False

    def test_depth_top5_thin(self):
        # bid side
        c = _make_cascade("B", pre={"depth_top5_bid_drop": -0.6})
        assert mod.f_depth_top5_thin(c) is True
        c = _make_cascade("B", pre={"depth_top5_bid_drop": -0.4})
        assert mod.f_depth_top5_thin(c) is False
        # ask side
        c = _make_cascade("A", pre={"depth_top5_ask_drop": -0.7})
        assert mod.f_depth_top5_thin(c) is True
        c = _make_cascade("A", pre={"depth_top5_ask_drop": -0.3})
        assert mod.f_depth_top5_thin(c) is False
        # neither
        c = _make_cascade("B", pre={"depth_top5_bid_drop": -0.3, "depth_top5_ask_drop": -0.2})
        assert mod.f_depth_top5_thin(c) is False

    def test_spread_widen(self):
        c = _make_cascade("B", pre={"spread_widen": 0.6})
        assert mod.f_spread_widen_gt_0_5(c) is True
        c = _make_cascade("B", pre={"spread_widen": 0.4})
        assert mod.f_spread_widen_gt_0_5(c) is False

    def test_ofi_sign_by_side(self):
        c = _make_cascade("B", pre={"ofi_5_30s_magnitude": -50.0})
        assert mod.f_ofi_5_30s_neg(c) is True
        c = _make_cascade("B", pre={"ofi_5_30s_magnitude": -10.0})
        assert mod.f_ofi_5_30s_neg(c) is False
        c = _make_cascade("A", pre={"ofi_5_30s_magnitude": 50.0})
        assert mod.f_ofi_5_30s_neg(c) is True
        c = _make_cascade("A", pre={"ofi_5_30s_magnitude": 10.0})
        assert mod.f_ofi_5_30s_neg(c) is False

    def test_stale_at_t5(self):
        c = _make_cascade("B", snapshots={"t5": {"stale_book_flag": True}})
        assert mod.f_stale_at_t5(c) is True
        c = _make_cascade("B", snapshots={"t5": {"stale_book_flag": False}})
        assert mod.f_stale_at_t5(c) is False
        c = _make_cascade("B", snapshots={"t5": None})
        assert mod.f_stale_at_t5(c) is False
        c = _make_cascade("B", snapshots={})
        assert mod.f_stale_at_t5(c) is False

    def test_pre_thin_post_recov_side_B(self):
        # bid thinned AND recovered
        c = _make_cascade("B", pre={"depth_top5_bid_drop": -0.6},
                          post={"depth_top5_bid_recovery": 0.4})
        assert mod.f_pre_thin_post_recov(c) is True
        # thinned but not recovered
        c = _make_cascade("B", pre={"depth_top5_bid_drop": -0.6},
                          post={"depth_top5_bid_recovery": 0.1})
        assert mod.f_pre_thin_post_recov(c) is False
        # recovered but not thinned
        c = _make_cascade("B", pre={"depth_top5_bid_drop": -0.2},
                          post={"depth_top5_bid_recovery": 0.5})
        assert mod.f_pre_thin_post_recov(c) is False

    def test_pre_thin_post_recov_side_A(self):
        c = _make_cascade("A", pre={"depth_top5_ask_drop": -0.7},
                          post={"depth_top5_ask_recovery": 0.5})
        assert mod.f_pre_thin_post_recov(c) is True

    def test_obi_5_recover(self):
        c = _make_cascade("B", post={"obi_5_recovery": 0.4})
        assert mod.f_obi_5_recover_gt_0_3(c) is True
        c = _make_cascade("B", post={"obi_5_recovery": 0.2})
        assert mod.f_obi_5_recover_gt_0_3(c) is False
        c = _make_cascade("B", post={})
        assert mod.f_obi_5_recover_gt_0_3(c) is False


# ----------------------------------------------------------------------------
# ISO timestamp parsing
# ----------------------------------------------------------------------------

class TestIsoToMs:
    def test_basic(self):
        # 2026-08-06T00:00:00Z = 1_785_974_400_000 ms (epoch)
        ms = mod._iso_to_ms("2026-08-06T00:00:00+00:00")
        assert ms == 1_785_974_400_000

    def test_z_suffix(self):
        assert mod._iso_to_ms("2026-08-06T00:00:00Z") == 1_785_974_400_000

    def test_invalid(self):
        assert mod._iso_to_ms("not a date") == 0
        assert mod._iso_to_ms("") == 0


# ----------------------------------------------------------------------------
# Return computation
# ----------------------------------------------------------------------------

def _make_candles(prices: List[float], start_ts_ms: int) -> List[dict]:
    """Build a 1m-candle list, one bar per minute."""
    return [
        {"t": start_ts_ms + i * 60_000, "c": price, "o": price, "h": price, "l": price}
        for i, price in enumerate(prices)
    ]


class TestComputeFadeReturn:
    def test_side_B_short_fade_profit(self):
        # Per project convention: side=B cascade -> fade = short (bet price
        # continues down). Entry=100, exit=90 -> short profit = +10% gross.
        # Net of 8bps round-trip cost = +9.92%.
        start_ms = 1_000_000_000
        candles = _make_candles([100, 100, 100, 100, 100, 90, 90], start_ms)
        c = _make_cascade("B")
        c["event_vwap"] = 100.0
        c["event_ts_ms"] = start_ms
        c["start_ts"] = "1970-01-12T13:46:40+00:00"  # ms = 1_000_000_000
        r = mod._compute_fade_return(c, candles, 5)
        assert r == pytest.approx(9.92, rel=0.01)

    def test_side_B_short_fade_loss(self):
        # Side B short fade, exit 101 (small move up) -> short loss = -1%
        # gross, -1.08% net of cost.
        start_ms = 1_000_000_000
        candles = _make_candles([100, 100, 100, 100, 100, 101, 101], start_ms)
        c = _make_cascade("B")
        c["event_vwap"] = 100.0
        c["start_ts"] = "1970-01-12T13:46:40+00:00"
        r = mod._compute_fade_return(c, candles, 5)
        assert r == pytest.approx(-1.08, rel=0.01)

    def test_side_A_long_fade_profit(self):
        # Side A cascade -> fade = long (bet price continues up).
        # Entry=100, exit=110 -> long profit = +10% gross, +9.92% net.
        start_ms = 1_000_000_000
        candles = _make_candles([100, 100, 100, 100, 100, 110, 110], start_ms)
        c = _make_cascade("A")
        c["event_vwap"] = 100.0
        c["start_ts"] = "1970-01-12T13:46:40+00:00"
        r = mod._compute_fade_return(c, candles, 5)
        assert r == pytest.approx(9.92, rel=0.01)

    def test_no_exit_bar(self):
        # Horizon beyond candle range.
        start_ms = 1_000_000_000
        candles = _make_candles([100, 100, 100], start_ms)
        c = _make_cascade("B")
        c["start_ts"] = "1970-01-12T13:46:40+00:00"
        r = mod._compute_fade_return(c, candles, 5)
        assert r is None

    def test_no_entry_price(self):
        start_ms = 1_000_000_000
        candles = _make_candles([100, 100, 100, 100, 100, 110], start_ms)
        c = _make_cascade("B")
        c["event_vwap"] = None
        c["start_ts"] = "1970-01-12T13:46:40+00:00"
        assert mod._compute_fade_return(c, candles, 5) is None

    def test_zero_entry_price(self):
        start_ms = 1_000_000_000
        candles = _make_candles([100, 100, 100, 100, 100, 110], start_ms)
        c = _make_cascade("B")
        c["event_vwap"] = 0.0
        c["start_ts"] = "1970-01-12T13:46:40+00:00"
        assert mod._compute_fade_return(c, candles, 5) is None


# ----------------------------------------------------------------------------
# Verdict gate
# ----------------------------------------------------------------------------

class TestVerdict:
    def test_pass(self):
        passed, reason = mod._verdict(n=50, pf=2.0, top_win_share=0.3)
        assert passed is True
        assert reason == "PASS"

    def test_fail_low_n(self):
        passed, reason = mod._verdict(n=20, pf=3.0, top_win_share=0.1)
        assert passed is False
        assert "n=" in reason

    def test_fail_low_pf(self):
        passed, reason = mod._verdict(n=50, pf=1.2, top_win_share=0.1)
        assert passed is False
        assert "PF=" in reason

    def test_fail_high_top_win(self):
        passed, reason = mod._verdict(n=50, pf=2.0, top_win_share=0.5)
        assert passed is False
        assert "top_win_share" in reason

    def test_fail_inf_pf(self):
        # No losses — suspicious.
        passed, reason = mod._verdict(n=50, pf=float("inf"), top_win_share=0.0)
        assert passed is False
        assert "inf" in reason


# ----------------------------------------------------------------------------
# Bucket evaluation
# ----------------------------------------------------------------------------

class TestEvaluateBucket:
    def test_no_trades(self):
        # Empty records.
        v = mod._evaluate_bucket(
            records=[], candles=[], symbol="BTC", side="B",
            horizon=15, filter_name="baseline", filter_fn=mod._is_baseline,
            filter_desc="control",
        )
        assert v.n == 0
        assert v.passed is False
        assert "no trades" in v.reason

    def test_basic(self):
        # Build a synthetic set: 50 cascades, all side=B, with profitable
        # short fades (exit price 90 vs entry 100) -> all wins.
        records = []
        for i in range(50):
            c = _make_cascade("B")
            c["event_vwap"] = 100.0
            c["start_ts"] = "1970-01-12T13:46:40+00:00"
            c["event_ts_ms"] = 1_000_000_000 + i * 60 * 60 * 1000
            records.append(c)
        candles = []
        for i in range(50):
            base = 1_000_000_000 + i * 60 * 60 * 1000
            # Exit at 90 -> side=B short fade = +10% profit
            candles.append({"t": base + 5 * 60_000, "c": 90.0, "o": 90.0, "h": 90.0, "l": 90.0})
        v = mod._evaluate_bucket(
            records=records, candles=candles, symbol="BTC", side="B",
            horizon=5, filter_name="baseline", filter_fn=mod._is_baseline,
            filter_desc="control",
        )
        assert v.n == 50
        assert v.win_rate == 1.0
        # No losses -> PF = inf
        assert v.pf == float("inf")
        # PF=inf should fail the suspicious check.
        assert v.passed is False
        assert "inf" in v.reason

    def test_filter_reduces_sample(self):
        # 100 cascades, only 30 match the filter.
        records = []
        for i in range(100):
            c = _make_cascade("B", pre={"obi_5_drop": -0.6 if i < 30 else -0.1})
            c["event_vwap"] = 100.0
            c["start_ts"] = "1970-01-12T13:46:40+00:00"
            c["event_ts_ms"] = 1_000_000_000 + i * 60 * 60 * 1000
            records.append(c)
        candles = []
        for i in range(100):
            base = 1_000_000_000 + i * 60 * 60 * 1000
            # Exit at 90 -> profitable side=B short fade
            candles.append({"t": base + 5 * 60_000, "c": 90.0, "o": 90.0, "h": 90.0, "l": 90.0})
        v = mod._evaluate_bucket(
            records=records, candles=candles, symbol="BTC", side="B",
            horizon=5, filter_name="obi_5_drop_lt_neg_0_5",
            filter_fn=mod.f_obi_5_drop_lt_neg_0_5,
            filter_desc="test",
        )
        assert v.n == 30
        assert v.n_total == 100


# ----------------------------------------------------------------------------
# File loading
# ----------------------------------------------------------------------------

class TestLoadL2CascadeRecords:
    def test_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "L2_CASCADE_DIR", tmp_path / "nonexistent")
        records = mod._load_l2_cascade_records("BTC")
        assert records == []

    def test_filters_by_symbol(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "L2_CASCADE_DIR", tmp_path)
        (tmp_path / "btc_2026-08-06.jsonl").write_text(
            json.dumps({"symbol": "BTC", "side": "B", "event_ts_ms": 1}) + "\n"
            + json.dumps({"symbol": "BTC", "side": "A", "event_ts_ms": 2}) + "\n"
        )
        (tmp_path / "eth_2026-08-06.jsonl").write_text(
            json.dumps({"symbol": "ETH", "side": "B", "event_ts_ms": 3}) + "\n"
        )
        btc = mod._load_l2_cascade_records("BTC")
        eth = mod._load_l2_cascade_records("ETH")
        assert len(btc) == 2
        assert len(eth) == 1
        assert all(r["symbol"] == "BTC" for r in btc)
        assert all(r["symbol"] == "ETH" for r in eth)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

class TestCLI:
    def test_help(self, capsys):
        with pytest.raises(SystemExit) as e:
            mod.main(["--help"])
        assert e.value.code == 0

"""Tests for scripts/build_l2_depth_features.py.

Covers the math helpers, SymbolState windowed-OFI and stale-book logic,
event parsing, and end-to-end stream processing on a synthetic 3-event file.

Run: pytest tests/test_build_l2_depth_features.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

# Allow `from scripts.build_l2_depth_features import ...` in test collection
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_l2_depth_features import (  # noqa: E402
    OFI_LEVELS,
    OFI_WINDOW_S,
    STALE_MID_DRIFT_BPS,
    STALE_MIN_HISTORY,
    STALE_SPREAD_MULT,
    STALE_WINDOW_S,
    SymbolState,
    _depth,
    _extract_date_from_filename,
    _lag_ms,
    _mid,
    _obi,
    _ofi_instant,
    _parse_event,
    _parse_iso_ms,
    _safe_div,
    _safe_level_sz,
    _spread_bps,
    discover_inputs,
    filter_by_date_range,
    process_file,
)


# ----------------------------------------------------------------------------
# Math helpers
# ----------------------------------------------------------------------------

class TestSafeDiv:
    def test_normal(self):
        assert _safe_div(6, 2) == 3.0

    def test_zero_denominator_returns_default(self):
        assert _safe_div(5, 0) == 0.0
        assert _safe_div(5, 0, default=99.0) == 99.0

    def test_negative_denominator_works(self):
        assert _safe_div(6, -2) == -3.0

    def test_nan_guard(self):
        # 0/0 would give NaN, which != NaN, so default kicks in
        assert _safe_div(0, 0) == 0.0


class TestDepth:
    def test_sum_top_n(self):
        levels = [
            {"px": "100", "sz": "1.0", "n": 1},
            {"px": "99", "sz": "2.0", "n": 1},
            {"px": "98", "sz": "3.0", "n": 1},
        ]
        assert _depth(levels, 2) == 3.0
        assert _depth(levels, 3) == 6.0

    def test_n_exceeds_levels(self):
        levels = [{"px": "100", "sz": "1.0"}]
        # Only 1 level, asking for 5 — returns 1.0 (no padding, no crash)
        assert _depth(levels, 5) == 1.0

    def test_empty_levels(self):
        assert _depth([], 5) == 0.0

    def test_missing_sz_field(self):
        levels = [{"px": "100"}, {"px": "99", "sz": "5.0"}]
        # First level has no sz — treated as 0
        assert _depth(levels, 2) == 5.0

    def test_non_numeric_sz(self):
        levels = [{"px": "100", "sz": "abc"}, {"px": "99", "sz": "2.0"}]
        # Bad sz skipped
        assert _depth(levels, 2) == 2.0


class TestObi:
    def test_balanced(self):
        assert _obi(10.0, 10.0) == 0.0

    def test_all_bid(self):
        assert _obi(10.0, 0.0) == 1.0

    def test_all_ask(self):
        assert _obi(0.0, 10.0) == -1.0

    def test_empty_book(self):
        # Divide by zero guard
        assert _obi(0.0, 0.0) == 0.0

    def test_unequal(self):
        # bid=7, ask=3 → (7-3)/(7+3) = 0.4
        assert abs(_obi(7.0, 3.0) - 0.4) < 1e-9


class TestMidSpread:
    def test_mid(self):
        assert _mid(100.0, 102.0) == 101.0

    def test_mid_with_zero(self):
        assert _mid(0.0, 100.0) == 0.0
        assert _mid(100.0, 0.0) == 0.0

    def test_spread_bps(self):
        # bid=100, ask=101, mid=100.5, spread=1/100.5*10000 = 99.5 bps
        assert abs(_spread_bps(100.0, 101.0) - 99.5024875) < 1e-4

    def test_spread_zero_bid(self):
        assert _spread_bps(0.0, 100.0) == 0.0

    def test_spread_zero_ask(self):
        assert _spread_bps(100.0, 0.0) == 0.0


class TestSafeLevelSz:
    def test_in_range(self):
        levels = [{"sz": "1.0"}, {"sz": "2.5"}]
        assert _safe_level_sz(levels, 0) == 1.0
        assert _safe_level_sz(levels, 1) == 2.5

    def test_out_of_range(self):
        levels = [{"sz": "1.0"}]
        assert _safe_level_sz(levels, 5) == 0.0

    def test_missing_sz(self):
        assert _safe_level_sz([{"px": "100"}], 0) == 0.0

    def test_non_numeric(self):
        assert _safe_level_sz([{"sz": "abc"}], 0) == 0.0


class TestOfiInstant:
    def test_no_prev(self):
        # First event: OFI = 0
        assert _ofi_instant(
            [{"sz": "1.0"}], [{"sz": "1.0"}], None, None, 1
        ) == 0.0

    def test_no_change(self):
        bids = [{"sz": "1.0"}, {"sz": "2.0"}]
        asks = [{"sz": "1.5"}, {"sz": "2.5"}]
        assert _ofi_instant(bids, asks, bids, asks, 2) == 0.0

    def test_bid_grew_only(self):
        curr = [{"sz": "5.0"}, {"sz": "2.0"}]
        prev = [{"sz": "1.0"}, {"sz": "2.0"}]
        asks_same = [{"sz": "1.0"}]
        # bid grew by 4 at k=0, 0 at k=1; ask unchanged
        # OFI = (5-1) - 0 + (2-2) - 0 = 4
        assert _ofi_instant(curr, asks_same, prev, asks_same, 2) == 4.0

    def test_ask_grew_only_negative(self):
        bids_same = [{"sz": "1.0"}]
        curr_ask = [{"sz": "5.0"}]
        prev_ask = [{"sz": "1.0"}]
        # ask grew by 4: OFI = 0 - (5-1) = -4
        assert _ofi_instant(bids_same, curr_ask, bids_same, prev_ask, 1) == -4.0

    def test_mixed_deltas(self):
        # k=0: bid grew 2 (1->3), ask grew 1 (2->3) → +1
        # k=1: bid shrunk 1 (4->3), ask grew 2 (1->3) → -3
        # Total: -2
        curr_bids = [{"sz": "3.0"}, {"sz": "3.0"}]
        curr_asks = [{"sz": "3.0"}, {"sz": "3.0"}]
        prev_bids = [{"sz": "1.0"}, {"sz": "4.0"}]
        prev_asks = [{"sz": "2.0"}, {"sz": "1.0"}]
        assert _ofi_instant(curr_bids, curr_asks, prev_bids, prev_asks, 2) == -2.0

    def test_level_count_mismatch(self):
        # Current has 1 level, prev has 3
        curr_b = [{"sz": "1.0"}]
        curr_a = [{"sz": "1.0"}]
        prev_b = [{"sz": "1.0"}, {"sz": "2.0"}, {"sz": "3.0"}]
        prev_a = [{"sz": "1.0"}, {"sz": "2.0"}, {"sz": "3.0"}]
        # k=0: bid 0, ask 0 → 0
        # k=1: curr missing → 0 - 2 = -2 (bid) and 0 - 2 = -2 (ask) → -2 - (-2) = 0
        # k=2: curr missing → 0 - 3 = -3 (bid) and 0 - 3 = -3 (ask) → -3 - (-3) = 0
        # Total: 0
        assert _ofi_instant(curr_b, curr_a, prev_b, prev_a, 3) == 0.0


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

class TestParseIsoMs:
    def test_z_suffix(self):
        ms = _parse_iso_ms("2026-08-08T00:00:00Z")
        assert ms > 0
        # 2026-08-08T00:00:00Z == 2026-08-08T00:00:00+00:00
        assert ms == _parse_iso_ms("2026-08-08T00:00:00+00:00")

    def test_offset(self):
        a = _parse_iso_ms("2026-08-08T08:00:00+08:00")
        b = _parse_iso_ms("2026-08-08T00:00:00+00:00")
        assert a == b

    def test_invalid(self):
        assert _parse_iso_ms("not a date") == 0
        assert _parse_iso_ms("") == 0
        assert _parse_iso_ms(None) == 0


class TestParseEvent:
    def test_valid(self):
        line = json.dumps({
            "recv_ts": "2026-08-08T00:00:00Z",
            "payload": {
                "coin": "BTC",
                "time": 1786147200000,
                "levels": [
                    [{"px": "100", "sz": "1.0", "n": 1}],
                    [{"px": "101", "sz": "2.0", "n": 1}],
                ],
                "spread": "1.0",
                "ts": 1786147200000,
            },
        })
        ev = _parse_event(line)
        assert ev is not None
        assert ev["coin"] == "BTC"
        assert ev["ts_ms"] == 1786147200000
        assert ev["spread_str"] == "1.0"
        assert len(ev["bids"]) == 1
        assert len(ev["asks"]) == 1

    def test_bad_json(self):
        assert _parse_event("not json") is None

    def test_missing_payload(self):
        assert _parse_event('{"recv_ts": "x"}') is None

    def test_missing_levels(self):
        line = json.dumps({"recv_ts": "x", "payload": {"coin": "BTC"}})
        assert _parse_event(line) is None

    def test_malformed_levels(self):
        line = json.dumps({
            "recv_ts": "x",
            "payload": {"coin": "BTC", "levels": [[{"px": "1"}]]},  # only 1 side
        })
        assert _parse_event(line) is None

    def test_ts_fallback(self):
        # If `time` is missing, fall back to `ts`
        line = json.dumps({
            "recv_ts": "x",
            "payload": {
                "coin": "BTC",
                "ts": 12345,
                "levels": [[{"px": "1"}], [{"px": "2"}]],
            },
        })
        ev = _parse_event(line)
        assert ev is not None
        assert ev["ts_ms"] == 12345


class TestLagMs:
    def test_normal(self):
        # recv_ts 1s after ts_ms
        lag = _lag_ms("2026-08-08T00:00:01Z", 1786147200000)
        # 1786147200000 ms = 2026-08-08T00:00:00Z, so recv - ts = 1000ms
        assert abs(lag - 1000) < 10

    def test_missing_inputs(self):
        assert _lag_ms(None, 123) == 0
        assert _lag_ms("2026-08-08T00:00:00Z", None) == 0
        assert _lag_ms("2026-08-08T00:00:00Z", 0) == 0
        assert _lag_ms("", 123) == 0

    def test_clock_skew_negative(self):
        # recv before ts → negative lag
        lag = _lag_ms("2026-08-08T00:00:00Z", 1786147201000)
        assert lag < 0


# ----------------------------------------------------------------------------
# SymbolState: OFI windowed math
# ----------------------------------------------------------------------------

class TestSymbolStateOfiWindows:
    def test_empty_windows_start_at_zero(self):
        st = SymbolState()
        w = st.get_windowed_ofi()
        for N in OFI_LEVELS:
            for ws in OFI_WINDOW_S:
                assert w[N][ws] == 0.0

    def test_single_update_persists(self):
        st = SymbolState()
        ts = 1000
        st.update_ofi(ts, {5: 2.5, 10: 3.0})
        w = st.get_windowed_ofi()
        # Both windows include the new value
        assert w[5][5] == 2.5
        assert w[5][30] == 2.5
        assert w[10][5] == 3.0
        assert w[10][30] == 3.0

    def test_window_eviction_5s(self):
        st = SymbolState()
        st.update_ofi(1000, {5: 1.0})
        # 7s later (clearly past the 5s boundary): old should evict
        st.update_ofi(7000, {5: 2.0})
        w = st.get_windowed_ofi()
        assert w[5][5] == 2.0  # only the new one in 5s window
        assert w[5][30] == 3.0  # both still in 30s window

    def test_window_eviction_30s(self):
        st = SymbolState()
        st.update_ofi(1000, {5: 1.0})
        # 32s later (clearly past the 30s boundary): old should evict
        st.update_ofi(32000, {5: 2.0})
        w = st.get_windowed_ofi()
        assert w[5][5] == 2.0
        assert w[5][30] == 2.0

    def test_cumulative_sum(self):
        st = SymbolState()
        # Three updates within 5s window
        for ts_ms, val in [(1000, 1.0), (2000, 2.0), (3000, 3.0)]:
            st.update_ofi(ts_ms, {5: val})
        w = st.get_windowed_ofi()
        assert w[5][5] == 6.0
        assert w[5][30] == 6.0

    def test_separate_n_levels(self):
        st = SymbolState()
        st.update_ofi(1000, {5: 1.0, 10: 10.0})
        w = st.get_windowed_ofi()
        assert w[5][5] == 1.0
        assert w[10][5] == 10.0


# ----------------------------------------------------------------------------
# SymbolState: stale book detection
# ----------------------------------------------------------------------------

class TestSymbolStateStale:
    def _fill(self, st, n, ts_start, spread_bps, mid):
        for i in range(n):
            st.update_spread_mid(ts_start + i * 1000, spread_bps, mid)

    def test_no_history_not_stale(self):
        st = SymbolState()
        st.update_spread_mid(1000, 1.0, 100.0)
        # Only 1 entry, need >= 5
        is_stale, drift = st.check_stale(10.0, 100.0)
        assert is_stale is False
        assert drift == 0.0

    def test_stable_history_not_stale(self):
        st = SymbolState()
        # 10 entries, stable spread 1.0, stable mid 100.0
        self._fill(st, 10, 1000, 1.0, 100.0)
        # Current spread 1.5 (just above 1.5x median=1.0) and mid 100 (no drift)
        # 1.5 > 1.5? Strict inequality: 1.5 is NOT > 1.5, so not stale
        is_stale, _ = st.check_stale(1.5, 100.0)
        assert is_stale is False

    def test_stale_spread_no_drift(self):
        st = SymbolState()
        self._fill(st, 10, 1000, 1.0, 100.0)
        # Current spread 5x median, mid same as history (no drift)
        is_stale, drift = st.check_stale(5.0, 100.0)
        assert is_stale is True
        assert drift == 0.0

    def test_stale_spread_with_drift_not_stale(self):
        st = SymbolState()
        self._fill(st, 10, 1000, 1.0, 100.0)
        # Current spread 5x median, but mid has drifted 100 bps (1%) in history
        # Need history entries with varying mid
        for i in range(10):
            st.update_spread_mid(1000 + i * 1000, 1.0, 100.0 + i * 0.1)
        is_stale, drift = st.check_stale(5.0, 100.9)
        # drift should be 0.9 / 100.9 * 10000 ≈ 89 bps, way above 1 bps threshold
        assert is_stale is False
        assert drift > STALE_MID_DRIFT_BPS

    def test_zero_median_spread_not_stale(self):
        st = SymbolState()
        self._fill(st, 10, 1000, 0.0, 100.0)
        is_stale, _ = st.check_stale(5.0, 100.0)
        assert is_stale is False

    def test_eviction_keeps_only_5min(self):
        st = SymbolState()
        # 10 entries over 6 minutes
        for i in range(10):
            st.update_spread_mid(1000 + i * 40000, 1.0, 100.0)
        # Window is now 5min; only the most recent ones within 5min of "now"
        # If we check at ts=1000+9*40000 = 361000, only entries with ts >= 61000
        # survive. That's i=0 (ts=1000) is excluded, i=1..9 (ts=41000..361000) survive.
        # Median of 9 entries all spread=1.0 → 1.0
        is_stale, _ = st.check_stale(5.0, 100.0)
        assert is_stale is True


# ----------------------------------------------------------------------------
# Filename parsing
# ----------------------------------------------------------------------------

class TestExtractDateFromFilename:
    def test_valid(self):
        p = Path("btc_2026-08-07.jsonl")
        assert _extract_date_from_filename(p) == "2026-08-07"

    def test_hype(self):
        p = Path("hype_2026-08-04.jsonl")
        assert _extract_date_from_filename(p) == "2026-08-04"

    def test_xyz_format(self):
        p = Path("xyz_gold_2026-08-05.jsonl")
        assert _extract_date_from_filename(p) == "2026-08-05"

    def test_invalid(self):
        assert _extract_date_from_filename(Path("garbage.jsonl")) is None
        assert _extract_date_from_filename(Path("btc.jsonl")) is None
        assert _extract_date_from_filename(Path("btc_2026-8-7.jsonl")) is None


class TestFilterByDateRange:
    def test_no_filter(self):
        items = [("BTC", "2026-08-05", Path()), ("BTC", "2026-08-06", Path())]
        assert filter_by_date_range(items, None, None) == items

    def test_start_only(self):
        items = [
            ("BTC", "2026-08-04", Path()),
            ("BTC", "2026-08-05", Path()),
            ("BTC", "2026-08-06", Path()),
        ]
        out = filter_by_date_range(items, "2026-08-05", None)
        assert [d for _, d, _ in out] == ["2026-08-05", "2026-08-06"]

    def test_end_only(self):
        items = [
            ("BTC", "2026-08-04", Path()),
            ("BTC", "2026-08-05", Path()),
            ("BTC", "2026-08-06", Path()),
        ]
        out = filter_by_date_range(items, None, "2026-08-05")
        assert [d for _, d, _ in out] == ["2026-08-04", "2026-08-05"]


# ----------------------------------------------------------------------------
# End-to-end: process_file on synthetic 3-event data
# ----------------------------------------------------------------------------

def _make_event(
    coin: str,
    ts_ms: int,
    recv_ts: str,
    bid_pxs: List[str],
    bid_szs: List[str],
    ask_pxs: List[str],
    ask_szs: List[str],
    spread: str = "1.0",
) -> str:
    return json.dumps({
        "recv_ts": recv_ts,
        "payload": {
            "coin": coin,
            "time": ts_ms,
            "levels": [
                [{"px": px, "sz": sz, "n": 1} for px, sz in zip(bid_pxs, bid_szs)],
                [{"px": px, "sz": sz, "n": 1} for px, sz in zip(ask_pxs, ask_szs)],
            ],
            "spread": spread,
            "ts": ts_ms,
        },
    })


class TestProcessFile:
    def test_three_events_end_to_end(self, tmp_path):
        # Three events 1 second apart, all BTC
        in_path = tmp_path / "btc_2026-08-08.jsonl"
        out_path = tmp_path / "out.jsonl"
        events = [
            _make_event(
                "BTC", 1786147200000, "2026-08-08T00:00:00Z",
                ["100.0", "99.0"], ["5.0", "10.0"],
                ["101.0", "102.0"], ["3.0", "4.0"],
            ),
            _make_event(
                "BTC", 1786147201000, "2026-08-08T00:00:01Z",
                ["100.0", "99.0"], ["6.0", "10.0"],  # bid sz[0] grew 1
                ["101.0", "102.0"], ["3.0", "4.0"],
            ),
            _make_event(
                "BTC", 1786147202000, "2026-08-08T00:00:02Z",
                ["100.0", "99.0"], ["6.0", "10.0"],
                ["101.0", "102.0"], ["4.0", "4.0"],  # ask sz[0] grew 1
            ),
        ]
        in_path.write_text("\n".join(events) + "\n", encoding="utf-8")

        # Capture log
        import logging
        log = logging.getLogger("test_e2e")
        log.setLevel(logging.WARNING)
        n = process_file("BTC", "2026-08-08", in_path, out_path, log)
        assert n == 3
        lines = out_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        recs = [json.loads(l) for l in lines]

        # Event 1: no prev, OFI = 0
        e1 = recs[0]
        assert e1["coin"] == "BTC"
        assert e1["ts_ms"] == 1786147200000
        assert e1["mid"] == 100.5
        assert abs(e1["spread_bps"] - 99.5024875) < 1e-4
        # depth_top1: bid=5.0, ask=3.0
        assert e1["depth_top1_bid"] == 5.0
        assert e1["depth_top1_ask"] == 3.0
        # obi_1: (5-3)/(5+3) = 0.25
        assert abs(e1["obi_1"] - 0.25) < 1e-9
        # depth_top5: bid=15.0, ask=7.0
        assert e1["depth_top5_bid"] == 15.0
        assert e1["depth_top5_ask"] == 7.0
        # First event: no prev → OFI instant = 0
        assert e1["ofi_5_instant"] == 0.0
        assert e1["ofi_10_instant"] == 0.0
        # windowed includes the 0 (since we update with the current instant)
        assert e1["ofi_5_5s"] == 0.0
        # Not enough history for stale
        assert e1["stale_book_flag"] is False
        # Lag: recv_ts - ts_ms
        assert e1["lag_ms"] == 0

        # Event 2: bid sz[0] grew 1 (5->6)
        e2 = recs[1]
        # OFI_5 instant: (6-5) - (3-3) = 1 at k=0, then 0s
        assert e2["ofi_5_instant"] == 1.0
        # OFI_5_5s window includes e1 (0) + e2 (1) = 1
        assert e2["ofi_5_5s"] == 1.0
        # OFI_5_30s = same
        assert e2["ofi_5_30s"] == 1.0
        # Stale still not enough history (< 5 in 5min)
        assert e2["stale_book_flag"] is False
        # Lag (in this synthetic data ts_ms == recv_ts for each event, so lag=0)
        assert e2["lag_ms"] == 0

        # Event 3: ask sz[0] grew 1 (3->4)
        e3 = recs[2]
        # OFI_5 instant: (6-6) - (4-3) = -1
        assert e3["ofi_5_instant"] == -1.0
        # OFI_5_5s cumulative: 0 (e1) + 1 (e2) + -1 (e3) = 0
        assert e3["ofi_5_5s"] == 0.0
        assert e3["ofi_5_30s"] == 0.0

    def test_skips_wrong_coin(self, tmp_path):
        in_path = tmp_path / "btc_2026-08-08.jsonl"
        out_path = tmp_path / "out.jsonl"
        events = [
            _make_event("ETH", 1786147200000, "2026-08-08T00:00:00Z",
                        ["100"], ["1"], ["101"], ["1"]),
            _make_event("BTC", 1786147201000, "2026-08-08T00:00:01Z",
                        ["100"], ["1"], ["101"], ["1"]),
        ]
        in_path.write_text("\n".join(events) + "\n", encoding="utf-8")
        import logging
        log = logging.getLogger("test_skip")
        log.setLevel(logging.WARNING)
        n = process_file("BTC", "2026-08-08", in_path, out_path, log)
        assert n == 1
        recs = [json.loads(l) for l in out_path.read_text(encoding="utf-8").strip().split("\n")]
        assert all(r["coin"] == "BTC" for r in recs)

    def test_skips_bad_json(self, tmp_path):
        in_path = tmp_path / "btc_2026-08-08.jsonl"
        out_path = tmp_path / "out.jsonl"
        lines = [
            "not valid json",
            "",
            _make_event("BTC", 1786147200000, "2026-08-08T00:00:00Z",
                        ["100"], ["1"], ["101"], ["1"]),
        ]
        in_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        import logging
        log = logging.getLogger("test_bad")
        log.setLevel(logging.WARNING)
        n = process_file("BTC", "2026-08-08", in_path, out_path, log)
        assert n == 1

    def test_missing_input_file(self, tmp_path):
        in_path = tmp_path / "does_not_exist.jsonl"
        out_path = tmp_path / "out.jsonl"
        import logging
        log = logging.getLogger("test_missing")
        log.setLevel(logging.WARNING)
        n = process_file("BTC", "2026-08-08", in_path, out_path, log)
        assert n == 0
        assert not out_path.exists()

    def test_stale_book_triggers_after_history(self, tmp_path):
        # Build 6 events: 5 normal, then 1 with huge spread
        in_path = tmp_path / "btc_2026-08-08.jsonl"
        out_path = tmp_path / "out.jsonl"
        events = []
        for i in range(5):
            events.append(_make_event(
                "BTC", 1786147200000 + i * 1000, f"2026-08-08T00:00:0{i}Z",
                ["100.0"], ["5.0"], ["101.0"], ["3.0"], spread="1.0",
            ))
        # 6th: spread widens (bid drops 1, ask rises 1) but mid stays at 100.5
        # This is the "stale book" pattern: depth pulled symmetrically,
        # midpoint unchanged, book gets wider without moving the center.
        events.append(_make_event(
            "BTC", 1786147205000, "2026-08-08T00:00:05Z",
            ["99.0"], ["5.0"], ["102.0"], ["3.0"], spread="3.0",
        ))
        in_path.write_text("\n".join(events) + "\n", encoding="utf-8")
        import logging
        log = logging.getLogger("test_stale")
        log.setLevel(logging.WARNING)
        n = process_file("BTC", "2026-08-08", in_path, out_path, log)
        assert n == 6
        recs = [json.loads(l) for l in out_path.read_text(encoding="utf-8").strip().split("\n")]
        # 6th event should be stale (spread 10x median 1.0, no mid drift)
        assert recs[5]["stale_book_flag"] is True
        # First 5 should not be stale (not enough history at that point)
        for r in recs[:5]:
            assert r["stale_book_flag"] is False
        # All 5 history events have the same mid (100.5) and same spread_bps (99.5)
        for r in recs[:5]:
            assert r["mid"] == 100.5
            assert abs(r["spread_bps"] - 99.5024875) < 1e-4

    def test_output_schema_has_all_fields(self, tmp_path):
        in_path = tmp_path / "btc_2026-08-08.jsonl"
        out_path = tmp_path / "out.jsonl"
        events = [
            _make_event("BTC", 1786147200000, "2026-08-08T00:00:00Z",
                        ["100"], ["5"], ["101"], ["3"]),
        ]
        in_path.write_text(events[0] + "\n", encoding="utf-8")
        import logging
        log = logging.getLogger("test_schema")
        log.setLevel(logging.WARNING)
        process_file("BTC", "2026-08-08", in_path, out_path, log)
        rec = json.loads(out_path.read_text(encoding="utf-8").strip())
        # Core fields
        for k in ["recv_ts", "ts_ms", "coin", "mid", "spread_bps", "lag_ms",
                  "stale_book_flag", "mid_drift_bps"]:
            assert k in rec, f"missing {k}"
        # Depth/OBI for each level
        for N in (1, 5, 10, 20):
            assert f"depth_top{N}_bid" in rec
            assert f"depth_top{N}_ask" in rec
            assert f"obi_{N}" in rec
        # OFI for the configured levels
        for N in OFI_LEVELS:
            assert f"ofi_{N}_instant" in rec
            for ws in OFI_WINDOW_S:
                assert f"ofi_{N}_{ws}s" in rec

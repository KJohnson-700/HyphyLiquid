"""Tests for scripts/run_btc_ask_heavy_reclaim_join.py.

Covers:
  - BBO bucket classification (_bbo_bucket)
  - Reclaim detection on synthetic candle windows (_reclaim_detected)
  - Entry/exit price extraction (_entry_px, _exit_px)
  - Trade record net PnL calc with cost in bps (_trade_record)
  - Promotion gate logic (_evaluate_promotion)
  - Bucket aggregation summary math (_BucketAgg.summary)
  - Per-cascade processor populates the right buckets (_process_cascade)
  - Cascade/candle loaders parse JSONL safely (_load_cascades, _load_candles)
  - end-to-end main() on a small synthetic cascade file
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.run_btc_ask_heavy_reclaim_join import (  # noqa: E402
    ASK_HEAVY_THRESHOLD,
    BID_HEAVY_THRESHOLD,
    PROMOTION_N,
    PROMOTION_PF,
    PROMOTION_TOP_WIN_SHARE,
    ROUND_TRIP_COST_BPS,
    STOP_SLIPPAGE_BPS,
    _BucketAgg,
    _bbo_bucket,
    _close,
    _entry_px,
    _evaluate_promotion,
    _exit_px,
    _load_candles,
    _load_cascades,
    _process_cascade,
    _reclaim_detected,
    _trade_record,
    main,
)


# ----------------------------- helpers ----------------------------------- #


def _candle(t_seconds: int, close: float, *, open_: float | None = None) -> dict:
    """Make a 1m candle whose `t` field is t_seconds (epoch seconds)."""
    t_ms = t_seconds * 1000
    return {
        "t": t_ms,
        "c": close,
        "o": open_ if open_ is not None else close,
        "h": close,
        "l": close,
        "v": 0.0,
        "n": 1,
    }


# 2026-08-01T00:00:00Z in epoch seconds
_TEST_BASE_SECONDS = 1_785_542_400


# ----------------------------- _bbo_bucket ------------------------------- #


def test_bbo_bucket_ask_heavy_under_threshold() -> None:
    assert _bbo_bucket(0.30) == "ask_heavy"
    assert _bbo_bucket(0.0) == "ask_heavy"
    assert _bbo_bucket(0.4499) == "ask_heavy"


def test_bbo_bucket_bid_heavy_over_threshold() -> None:
    assert _bbo_bucket(0.70) == "bid_heavy"
    assert _bbo_bucket(1.0) == "bid_heavy"
    assert _bbo_bucket(0.5501) == "bid_heavy"


def test_bbo_bucket_balanced_in_band() -> None:
    assert _bbo_bucket(0.45) == "balanced"
    assert _bbo_bucket(0.50) == "balanced"
    assert _bbo_bucket(0.55) == "balanced"


def test_bbo_bucket_unknown_when_none() -> None:
    assert _bbo_bucket(None) == "unknown"


# ----------------------------- _reclaim_detected ------------------------- #


def test_reclaim_detected_b_side_returns_false_when_no_close_below_vwap() -> None:
    # B-side cascade pushed price up; reclaim = any close < vwap in wait window.
    base = _TEST_BASE_SECONDS  # 2026-08-01T00:00:00Z
    candles = [
        _candle(base, 100.0),
        _candle(base + 60, 100.5),
        _candle(base + 120, 100.4),
        _candle(base + 180, 100.7),
    ]
    assert _reclaim_detected(side="B", event_vwap=100.0, candles=candles, entry_idx=0, wait_minutes=3) is False


def test_reclaim_detected_b_side_returns_true_on_first_sub_vwap_close() -> None:
    base = _TEST_BASE_SECONDS
    candles = [
        _candle(base, 100.0),
        _candle(base + 60, 100.5),  # reclaim bar
        _candle(base + 120, 100.4),
    ]
    assert _reclaim_detected(side="B", event_vwap=101.0, candles=candles, entry_idx=0, wait_minutes=3) is True


def test_reclaim_detected_a_side_mirror() -> None:
    base = _TEST_BASE_SECONDS
    candles = [
        _candle(base, 100.0),
        _candle(base + 60, 101.5),  # reclaim: close > vwap
    ]
    assert _reclaim_detected(side="A", event_vwap=100.0, candles=candles, entry_idx=0, wait_minutes=3) is True


def test_reclaim_detected_skips_bad_closes() -> None:
    base = _TEST_BASE_SECONDS
    candles = [
        _candle(base, 100.0),
        {"t": (base + 60) * 1000, "c": 0},  # bad
        _candle(base + 120, 99.0),  # reclaim
    ]
    assert _reclaim_detected(side="B", event_vwap=101.0, candles=candles, entry_idx=0, wait_minutes=3) is True


def test_reclaim_detected_wait_caps_at_window_end() -> None:
    base = _TEST_BASE_SECONDS
    candles = [
        _candle(base, 100.0),
        _candle(base + 60, 99.0),  # reclaim here
        _candle(base + 120, 99.0),
    ]
    # wait_minutes=1 should only look at bar 0..entry+1
    assert _reclaim_detected(side="B", event_vwap=101.0, candles=candles, entry_idx=0, wait_minutes=1) is True


# ----------------------------- _entry_px / _exit_px --------------------- #


def test_entry_px_happy_path() -> None:
    candles = [_candle(100, 100.0), _candle(160, 101.0)]
    assert _entry_px(candles, 0) == 100.0
    assert _entry_px(candles, 1) == 101.0


def test_entry_px_returns_none_for_missing() -> None:
    assert _entry_px([{"t": 100, "o": 0}], 0) is None
    # empty list: returns None (not raise)
    assert _entry_px([], 0) is None


def test_exit_px_returns_none_when_past_end() -> None:
    candles = [_candle(100, 100.0), _candle(160, 101.0)]
    assert _exit_px(candles, 0, horizon=10) is None


def test_exit_px_returns_price_and_index() -> None:
    candles = [_candle(100, 100.0), _candle(160, 101.0), _candle(220, 102.0)]
    px, idx = _exit_px(candles, 0, horizon=1)
    assert px == 101.0
    assert idx == 1


# ----------------------------- _trade_record ----------------------------- #


def test_trade_record_long_net_subtracts_cost() -> None:
    rec = _trade_record(
        cascade={"event_vwap": 100.0, "top_book_imbalance": 0.30, "start_ts": "2026-08-01T00:00:00+00:00"},
        symbol="BTC",
        side="B",
        direction="long",
        entry_idx=0,
        exit_idx=15,
        entry_px=100.0,
        exit_px=101.0,
        bucket="ask_heavy_AND_failed_reclaim_continuation",
        reclaimed=False,
        bbo_bucket="ask_heavy",
    )
    # raw = (101-100)/100 * 100 = 1.0 %
    # cost = (8 + 2) / 100 = 0.10 %
    # net = 0.90
    assert rec["raw_pnl_pct"] == 1.0
    assert rec["net_pnl_pct"] == round(1.0 - (ROUND_TRIP_COST_BPS + STOP_SLIPPAGE_BPS) / 100.0, 4)


def test_trade_record_short_uses_short_formula() -> None:
    rec = _trade_record(
        cascade={"event_vwap": 100.0, "top_book_imbalance": 0.30, "start_ts": "2026-08-01T00:00:00+00:00"},
        symbol="BTC",
        side="A",
        direction="short",
        entry_idx=0,
        exit_idx=15,
        entry_px=100.0,
        exit_px=99.0,
        bucket="ask_heavy_AND_failed_reclaim_continuation",
        reclaimed=False,
        bbo_bucket="ask_heavy",
    )
    # raw short = (100-99)/100 * 100 = 1.0 %
    assert rec["raw_pnl_pct"] == 1.0
    assert rec["direction"] == "short"


# ----------------------------- _evaluate_promotion ----------------------- #


def test_promotion_gate_pass() -> None:
    passed, reason = _evaluate_promotion(n=50, pf=2.0, med=0.05, top_win_share=0.10)
    assert passed is True
    assert "all gates met" in reason


def test_promotion_gate_fail_low_n() -> None:
    passed, reason = _evaluate_promotion(n=20, pf=2.5, med=0.05, top_win_share=0.10)
    assert passed is False
    assert "sample too small" in reason


def test_promotion_gate_fail_low_pf() -> None:
    passed, reason = _evaluate_promotion(n=50, pf=1.2, med=0.05, top_win_share=0.10)
    assert passed is False
    assert "PF=" in reason


def test_promotion_gate_fail_negative_median() -> None:
    passed, reason = _evaluate_promotion(n=50, pf=2.0, med=-0.001, top_win_share=0.10)
    assert passed is False
    assert "median" in reason


def test_promotion_gate_fail_top_win_share_too_concentrated() -> None:
    passed, reason = _evaluate_promotion(n=50, pf=2.0, med=0.05, top_win_share=0.5)
    assert passed is False
    assert "top_win_share" in reason


def test_promotion_gate_treat_inf_pf_as_pass() -> None:
    passed, reason = _evaluate_promotion(n=50, pf=float("inf"), med=0.05, top_win_share=0.10)
    assert passed is True
    assert "inf" in reason


# ----------------------------- _BucketAgg -------------------------------- #


def test_bucket_agg_empty_summary() -> None:
    agg = _BucketAgg()
    s = agg.summary("BTC", "B", "test")
    assert s.n == 0
    assert s.passed is False
    assert "no events" in s.reason


def test_bucket_agg_pnl_math() -> None:
    agg = _BucketAgg()
    # 3 wins of 1.0%, 2 losses of 0.5%
    for _ in range(3):
        agg.add({"net_pnl_pct": 1.0})
    for _ in range(2):
        agg.add({"net_pnl_pct": -0.5})
    s = agg.summary("BTC", "B", "test")
    assert s.n == 5
    assert s.win_rate == 0.6
    # avg = (3 * 1.0 + 2 * -0.5) / 5 = (3 - 1) / 5 = 0.4
    assert s.avg_pnl_pct == 0.4
    # med of [-0.5, -0.5, 1.0, 1.0, 1.0] sorted is 1.0
    assert s.median_pnl_pct == 1.0
    # PF = gross_profit / gross_loss = 3.0 / 1.0 = 3.0
    assert abs(s.pf - 3.0) < 0.0001
    # top_win_share = 1.0 / 3.0 = 0.333...
    assert abs(s.top_win_share - 1 / 3) < 0.0001


def test_bucket_agg_zero_loss_gives_inf_pf() -> None:
    agg = _BucketAgg()
    agg.add({"net_pnl_pct": 1.0})
    agg.add({"net_pnl_pct": 0.5})
    s = agg.summary("BTC", "B", "test")
    assert s.pf == float("inf")


def test_bucket_agg_zero_zero_pf_is_zero() -> None:
    agg = _BucketAgg()
    agg.add({"net_pnl_pct": 0.0})
    s = agg.summary("BTC", "B", "test")
    assert s.pf == 0.0


# ----------------------------- _load_cascades ---------------------------- #


def test_load_cascades_parses_valid_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "cascades.jsonl"
    path.write_text(
        '{"symbol": "BTC", "side": "B", "event_vwap": 100.0}\n'
        '{"symbol": "ETH", "side": "A", "event_vwap": 200.0}\n'
        "not-json-line\n"
        "\n",
        encoding="utf-8",
    )
    rows = _load_cascades(path)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "BTC"
    assert rows[1]["side"] == "A"


def test_load_cascades_returns_empty_when_missing(tmp_path: Path) -> None:
    assert _load_cascades(tmp_path / "missing.jsonl") == []


# ----------------------------- _process_cascade -------------------------- #


def _build_aggs() -> dict[str, _BucketAgg]:
    return {
        "generic_failed_reclaim_continuation": _BucketAgg(),
        "ask_heavy_ANY": _BucketAgg(),
        "bid_heavy_ANY": _BucketAgg(),
        "ask_heavy_AND_always_fade": _BucketAgg(),
        "ask_heavy_AND_always_follow": _BucketAgg(),
        "ask_heavy_AND_failed_reclaim_continuation": _BucketAgg(),
        "bid_heavy_AND_failed_reclaim_continuation": _BucketAgg(),
    }


def test_process_cascade_ask_heavy_no_reclaim_joins_buckets() -> None:
    """BTC B-side cascade with ask-heavy book and no reclaim should populate
    generic, ask_heavy_AND_always_fade, ask_heavy_AND_always_follow, AND
    ask_heavy_AND_failed_reclaim_continuation (the join)."""
    # Pick a start_ts that maps to a candle open.
    base = _TEST_BASE_SECONDS  # 2026-08-01T00:00:00Z
    # Cascade happens at base; entry bar is base+60s.
    candles = [_candle(base + (i + 1) * 60, 100.0 + (0.05 * i if i < 5 else 0.0)) for i in range(20)]
    # B-side cascade: reclaim requires close < vwap=100; all closes >= 100.0
    cascade = {
        "symbol": "BTC",
        "side": "B",
        "event_vwap": 100.0,
        "top_book_imbalance": 0.30,  # ask_heavy
        "start_ts": "2026-08-01T00:00:00+00:00",
    }
    aggs = _build_aggs()
    _process_cascade(
        cascade,
        candles,
        symbol="BTC",
        side="B",
        horizon=15,
        wait=3,
        ask_heavy_threshold=ASK_HEAVY_THRESHOLD,
        bid_heavy_threshold=BID_HEAVY_THRESHOLD,
        agg_generic_failed_reclaim=aggs["generic_failed_reclaim_continuation"],
        agg_ask_heavy_any=aggs["ask_heavy_ANY"],
        agg_bid_heavy_any=aggs["bid_heavy_ANY"],
        agg_ask_heavy_and_failed_reclaim=aggs["ask_heavy_AND_failed_reclaim_continuation"],
        agg_bid_heavy_and_failed_reclaim=aggs["bid_heavy_AND_failed_reclaim_continuation"],
        agg_ask_heavy_and_always_fade=aggs["ask_heavy_AND_always_fade"],
        agg_ask_heavy_and_always_follow=aggs["ask_heavy_AND_always_follow"],
    )
    assert aggs["generic_failed_reclaim_continuation"].n == 1
    assert aggs["ask_heavy_AND_always_fade"].n == 1
    assert aggs["ask_heavy_AND_always_follow"].n == 1
    assert aggs["ask_heavy_AND_failed_reclaim_continuation"].n == 1
    # bid_heavy buckets should be empty
    assert aggs["bid_heavy_AND_failed_reclaim_continuation"].n == 0
    assert aggs["bid_heavy_ANY"].n == 0


def test_process_cascade_bid_heavy_with_no_reclaim_populates_bid_heavy_join() -> None:
    base = _TEST_BASE_SECONDS
    candles = [_candle(base + (i + 1) * 60, 100.0 + (0.05 * i if i < 5 else 0.0)) for i in range(20)]
    cascade = {
        "symbol": "BTC",
        "side": "B",
        "event_vwap": 100.0,
        "top_book_imbalance": 0.70,  # bid_heavy
        "start_ts": "2026-08-01T00:00:00+00:00",
    }
    aggs = _build_aggs()
    _process_cascade(
        cascade,
        candles,
        symbol="BTC",
        side="B",
        horizon=15,
        wait=3,
        ask_heavy_threshold=ASK_HEAVY_THRESHOLD,
        bid_heavy_threshold=BID_HEAVY_THRESHOLD,
        agg_generic_failed_reclaim=aggs["generic_failed_reclaim_continuation"],
        agg_ask_heavy_any=aggs["ask_heavy_ANY"],
        agg_bid_heavy_any=aggs["bid_heavy_ANY"],
        agg_ask_heavy_and_failed_reclaim=aggs["ask_heavy_AND_failed_reclaim_continuation"],
        agg_bid_heavy_and_failed_reclaim=aggs["bid_heavy_AND_failed_reclaim_continuation"],
        agg_ask_heavy_and_always_fade=aggs["ask_heavy_AND_always_fade"],
        agg_ask_heavy_and_always_follow=aggs["ask_heavy_AND_always_follow"],
    )
    # bid_heavy join is the only positive test
    assert aggs["bid_heavy_AND_failed_reclaim_continuation"].n == 1
    # ask_heavy variants are zero
    assert aggs["ask_heavy_AND_always_fade"].n == 0
    assert aggs["ask_heavy_AND_always_follow"].n == 0
    assert aggs["ask_heavy_AND_failed_reclaim_continuation"].n == 0
    # generic is still populated (no filter)
    assert aggs["generic_failed_reclaim_continuation"].n == 1


def test_process_cascade_reclaim_in_window_blocks_join_but_keeps_always_variants() -> None:
    """If reclaim happens in the wait window, the JOIN should NOT fire
    (failed_reclaim_continuation is the precondition), but the immediate
    always_fade / always_follow variants should still record the trade."""
    base = _TEST_BASE_SECONDS
    # entry bar is base+60s. candle[1] is base+120s. vwap=101, close=100 < 101 -> reclaim.
    candles = [
        _candle(base + 60, 100.5),
        _candle(base + 120, 100.0),  # reclaim: close < vwap 101 for B-side
        _candle(base + 180, 100.0),
    ] + [_candle(base + 240 + i * 60, 100.0) for i in range(20)]
    cascade = {
        "symbol": "BTC",
        "side": "B",
        "event_vwap": 101.0,  # reclaim happens because 100 < 101
        "top_book_imbalance": 0.30,
        "start_ts": "2026-08-01T00:00:00+00:00",
    }
    aggs = _build_aggs()
    _process_cascade(
        cascade,
        candles,
        symbol="BTC",
        side="B",
        horizon=15,
        wait=3,
        ask_heavy_threshold=ASK_HEAVY_THRESHOLD,
        bid_heavy_threshold=BID_HEAVY_THRESHOLD,
        agg_generic_failed_reclaim=aggs["generic_failed_reclaim_continuation"],
        agg_ask_heavy_any=aggs["ask_heavy_ANY"],
        agg_bid_heavy_any=aggs["bid_heavy_ANY"],
        agg_ask_heavy_and_failed_reclaim=aggs["ask_heavy_AND_failed_reclaim_continuation"],
        agg_bid_heavy_and_failed_reclaim=aggs["bid_heavy_AND_failed_reclaim_continuation"],
        agg_ask_heavy_and_always_fade=aggs["ask_heavy_AND_always_fade"],
        agg_ask_heavy_and_always_follow=aggs["ask_heavy_AND_always_follow"],
    )
    # generic is zero (reclaim happened, so failed_reclaim is False)
    assert aggs["generic_failed_reclaim_continuation"].n == 0
    # always variants recorded
    assert aggs["ask_heavy_AND_always_fade"].n == 1
    assert aggs["ask_heavy_AND_always_follow"].n == 1
    # but the JOIN (which requires no reclaim) is zero
    assert aggs["ask_heavy_AND_failed_reclaim_continuation"].n == 0


def test_process_cascade_no_imbalance_blocks_all_filter_buckets() -> None:
    base = _TEST_BASE_SECONDS
    candles = [_candle(base + (i + 1) * 60, 100.0) for i in range(20)]
    cascade = {
        "symbol": "BTC",
        "side": "B",
        "event_vwap": 100.0,
        "top_book_imbalance": None,  # no imbalance data
        "start_ts": "2026-08-01T00:00:00+00:00",
    }
    aggs = _build_aggs()
    _process_cascade(
        cascade,
        candles,
        symbol="BTC",
        side="B",
        horizon=15,
        wait=3,
        ask_heavy_threshold=ASK_HEAVY_THRESHOLD,
        bid_heavy_threshold=BID_HEAVY_THRESHOLD,
        agg_generic_failed_reclaim=aggs["generic_failed_reclaim_continuation"],
        agg_ask_heavy_any=aggs["ask_heavy_ANY"],
        agg_bid_heavy_any=aggs["bid_heavy_ANY"],
        agg_ask_heavy_and_failed_reclaim=aggs["ask_heavy_AND_failed_reclaim_continuation"],
        agg_bid_heavy_and_failed_reclaim=aggs["bid_heavy_AND_failed_reclaim_continuation"],
        agg_ask_heavy_and_always_fade=aggs["ask_heavy_AND_always_fade"],
        agg_ask_heavy_and_always_follow=aggs["ask_heavy_AND_always_follow"],
    )
    # Generic is still populated (no filter), but filtered buckets are zero
    assert aggs["generic_failed_reclaim_continuation"].n == 1
    assert aggs["ask_heavy_AND_always_fade"].n == 0
    assert aggs["ask_heavy_AND_always_follow"].n == 0
    assert aggs["ask_heavy_AND_failed_reclaim_continuation"].n == 0
    assert aggs["bid_heavy_AND_failed_reclaim_continuation"].n == 0


# ----------------------------- end-to-end main ---------------------------- #


def test_main_smoke_test_with_synthetic_cascades(tmp_path: Path, monkeypatch, capsys) -> None:
    """Run main() against a tiny synthetic cascades.jsonl and ws_candle dir,
    verify verdicts and JSON/MD outputs are written."""
    cascades_path = tmp_path / "cascades.jsonl"
    candle_dir = tmp_path / "ws_candle"

    base = _TEST_BASE_SECONDS  # 2026-08-01T00:00:00Z
    cascades = [
        # BTC B: ask_heavy, no reclaim, continuation goes up
        {
            "symbol": "BTC",
            "side": "B",
            "event_vwap": 100.0,
            "top_book_imbalance": 0.30,
            "start_ts": "2026-08-01T00:00:00+00:00",
        },
        # BTC B: bid_heavy, no reclaim (control)
        {
            "symbol": "BTC",
            "side": "B",
            "event_vwap": 100.0,
            "top_book_imbalance": 0.70,
            "start_ts": "2026-08-01T00:01:00+00:00",
        },
    ]
    cascades_path.write_text("\n".join(json.dumps(c) for c in cascades) + "\n", encoding="utf-8")

    # Candles: entry bar at base+60s, then 30 more.
    # B-side reclaim requires close < vwap=100. Stay above 100 throughout -> no reclaim.
    candle_records = []
    for i in range(30):
        t_ms = (base + (i + 1) * 60) * 1000
        c = 100.05 + (i * 0.02)
        candle_records.append(
            {
                "payload": {
                    "t": t_ms,
                    "T": t_ms + 59_999,
                    "s": "BTC",
                    "c": c,
                    "o": c,
                    "h": c,
                    "l": c,
                    "v": 0.0,
                    "n": 1,
                }
            }
        )
    candle_dir.mkdir(parents=True, exist_ok=True)
    (candle_dir / "btc_2026-08-01.jsonl").write_text(
        "\n".join(json.dumps(r) for r in candle_records) + "\n", encoding="utf-8"
    )

    # Patch the module's CASCADES_PATH, CANDLES_DIR, and sys.argv.
    import scripts.run_btc_ask_heavy_reclaim_join as mod

    monkeypatch.setattr(mod, "CASCADES_PATH", cascades_path)
    monkeypatch.setattr(mod, "CANDLES_DIR", candle_dir)
    out_json = tmp_path / "results.json"
    out_md = tmp_path / "summary.md"
    monkeypatch.setattr(mod, "RESULTS_JSON_PATH", out_json)
    monkeypatch.setattr(mod, "SUMMARY_MD_PATH", out_md)
    monkeypatch.setattr("sys.argv", ["run_btc_ask_heavy_reclaim_join.py", "--symbols", "BTC"])

    rc = mod.main()
    assert rc == 0
    assert out_json.exists()
    assert out_md.exists()
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert "verdicts" in payload
    assert "coverage" in payload
    # Both cascades should be evaluated
    coverage_btc_b = payload["coverage"]["BTC|B"]
    assert coverage_btc_b["total"] == 2
    assert coverage_btc_b["ask_heavy"] == 1
    assert coverage_btc_b["bid_heavy"] == 1
    # The verdicts list should include the ask_heavy join bucket
    join_verdicts = [
        v for v in payload["verdicts"] if v["bucket"] == "ask_heavy_AND_failed_reclaim_continuation"
    ]
    assert len(join_verdicts) >= 1
    btc_b_join = [v for v in join_verdicts if v["symbol"] == "BTC" and v["side"] == "B"][0]
    assert btc_b_join["n"] >= 1


def test_main_returns_zero_for_empty_cascades(tmp_path: Path, monkeypatch) -> None:
    """Empty cascades file should still produce output without raising."""
    import scripts.run_btc_ask_heavy_reclaim_join as mod

    cascades_path = tmp_path / "cascades.jsonl"
    cascades_path.write_text("", encoding="utf-8")
    candle_dir = tmp_path / "ws_candle"
    candle_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(mod, "CASCADES_PATH", cascades_path)
    monkeypatch.setattr(mod, "CANDLES_DIR", candle_dir)
    monkeypatch.setattr(mod, "RESULTS_JSON_PATH", tmp_path / "results.json")
    monkeypatch.setattr(mod, "SUMMARY_MD_PATH", tmp_path / "summary.md")
    monkeypatch.setattr("sys.argv", ["run_btc_ask_heavy_reclaim_join.py"])

    rc = mod.main()
    assert rc == 0


# ----------------------------- main helper test -------------------------- #


def test_close_helper() -> None:
    assert _close({"c": 100.5}) == 100.5
    assert _close({"payload": {"c": 99.0}}) == 99.0
    assert _close({"c": "bad"}) is None
    assert _close({}) is None

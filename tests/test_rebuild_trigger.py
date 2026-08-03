"""Tests for src/strategy/rebuild_trigger.py.

Covers:
  - _parse_ts_string: ISO 8601 strings, Unix ms/s ints and numerics
  - parse_liquidation_ts: top-level ts, top-level t/time, payload.ts, data.T
  - count_liquidations / last_liquidation_ts_ms
  - load_baseline / save_baseline (atomic write)
  - check_should_rebuild: HOLD on missing baseline, HOLD on insufficient rows,
    FIRE when all conditions met, daily fallback
  - update_baseline writes expected fields
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.strategy.rebuild_trigger import (
    BASELINE_PATH,
    LIQUIDATIONS_PATH,
    THRESHOLD_LAST_LIQ_AGE_MIN,
    THRESHOLD_LAST_REBUILD_AGE_MIN,
    THRESHOLD_NEW_ROWS,
    _parse_ts_string,
    check_should_rebuild,
    count_liquidations,
    last_liquidation_ts_ms,
    load_baseline,
    parse_liquidation_ts,
    save_baseline,
    count_mature_new_liquidations,
    update_baseline,
)

UTC = timezone.utc


# ----------------------------------------------------------------------------
# _parse_ts_string
# ----------------------------------------------------------------------------

class TestParseTsString:
    # 2026-08-02T19:10:37.000+00:00 -> 1785697837000 ms
    REF_MS = 1785697837000

    def test_iso8601_with_offset(self):
        ms = _parse_ts_string("2026-08-02T19:10:37.000+00:00")
        assert ms is not None
        assert abs(ms - self.REF_MS) < 1000

    def test_iso8601_z_suffix(self):
        ms = _parse_ts_string("2026-08-02T19:10:37Z")
        assert ms is not None
        assert abs(ms - self.REF_MS) < 1000

    def test_iso8601_naive_treated_as_utc(self):
        ms = _parse_ts_string("2026-08-02T19:10:37")
        assert ms is not None
        assert abs(ms - self.REF_MS) < 1000

    def test_unix_ms_int(self):
        assert _parse_ts_string(self.REF_MS) == self.REF_MS

    def test_unix_seconds_int(self):
        assert _parse_ts_string(self.REF_MS // 1000) == self.REF_MS

    def test_unix_ms_numeric_string(self):
        assert _parse_ts_string(str(self.REF_MS)) == self.REF_MS

    def test_unix_seconds_numeric_string(self):
        assert _parse_ts_string(str(self.REF_MS // 1000)) == self.REF_MS

    def test_none_returns_none(self):
        assert _parse_ts_string(None) is None

    def test_empty_returns_none(self):
        assert _parse_ts_string("") is None
        assert _parse_ts_string("   ") is None

    def test_garbage_returns_none(self):
        assert _parse_ts_string("not a date") is None

    def test_bool_returns_none(self):
        # bool would otherwise pass isinstance((int, float))
        assert _parse_ts_string(True) is None


# ----------------------------------------------------------------------------
# parse_liquidation_ts
# ----------------------------------------------------------------------------

class TestParseLiquidationTs:
    def test_top_level_ts_iso(self):
        line = json.dumps({"ts": "2026-08-02T19:10:37.000+00:00", "symbol": "BTC"})
        assert parse_liquidation_ts(line) is not None

    def test_top_level_t_int(self):
        line = json.dumps({"t": 1775152237000, "symbol": "ETH"})
        assert parse_liquidation_ts(line) == 1775152237000

    def test_top_level_time(self):
        line = json.dumps({"time": "2026-08-02T19:10:37Z"})
        assert parse_liquidation_ts(line) is not None

    def test_payload_nested(self):
        line = json.dumps({"payload": {"ts": "2026-08-02T19:10:37.000+00:00"}})
        assert parse_liquidation_ts(line) is not None

    def test_data_nested(self):
        line = json.dumps({"data": {"T": 1775152237000}})
        assert parse_liquidation_ts(line) == 1775152237000

    def test_no_ts_returns_none(self):
        line = json.dumps({"symbol": "BTC", "side": "A"})
        assert parse_liquidation_ts(line) is None

    def test_bad_json_returns_none(self):
        assert parse_liquidation_ts("not json") is None
        assert parse_liquidation_ts("") is None

    def test_realistic_hl_line(self):
        line = (
            '{"ts": "2026-08-03T03:13:07.621000+00:00", "symbol": "HYPE", '
            '"side": "A", "total_notional": 250668.30, "n_fills": 84}'
        )
        ms = parse_liquidation_ts(line)
        assert ms is not None
        assert ms > 1775000000000  # sanity: 2026-08-ish


# ----------------------------------------------------------------------------
# count_liquidations / last_liquidation_ts_ms
# ----------------------------------------------------------------------------

class TestLiquidationFileHelpers:
    def test_count_missing_file(self, tmp_path):
        assert count_liquidations(tmp_path / "nope.jsonl") == 0

    def test_count_basic(self, tmp_path):
        p = tmp_path / "liq.jsonl"
        p.write_text('{"ts": "2026-08-02T19:00:00+00:00"}\n{"ts": "2026-08-02T20:00:00+00:00"}\n')
        assert count_liquidations(p) == 2

    def test_count_ignores_blank_lines(self, tmp_path):
        p = tmp_path / "liq.jsonl"
        p.write_text('{"ts": "2026-08-02T19:00:00+00:00"}\n\n{"ts": "2026-08-02T20:00:00+00:00"}\n')
        assert count_liquidations(p) == 2

    def test_last_ts_missing(self, tmp_path):
        assert last_liquidation_ts_ms(tmp_path / "nope.jsonl") is None

    def test_last_ts_returns_last_line(self, tmp_path):
        # File is append-only with chronological writes, so "last line" is
        # also the most recent. Order: 19:00, 20:00, 20:30.
        p = tmp_path / "liq.jsonl"
        p.write_text(
            '{"ts": "2026-08-02T19:00:00+00:00"}\n'
            '{"ts": "2026-08-02T20:00:00+00:00"}\n'
            '{"ts": "2026-08-02T20:30:00+00:00"}\n'
        )
        ms = last_liquidation_ts_ms(p)
        assert ms is not None
        dt = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
        assert dt.hour == 20 and dt.minute == 30

    def test_last_ts_skips_unparseable(self, tmp_path):
        p = tmp_path / "liq.jsonl"
        p.write_text(
            'not json\n'
            '{"no_ts": true}\n'
            '{"ts": "2026-08-02T20:00:00+00:00"}\n'
        )
        ms = last_liquidation_ts_ms(p)
        assert ms is not None
        dt = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
        assert dt.hour == 20

    def test_count_mature_new_liquidations_skips_fresh_tail(self, tmp_path):
        p = tmp_path / "liq.jsonl"
        now = datetime(2026, 8, 3, 21, 0, tzinfo=UTC)
        rows = [
            {"ts": (now - timedelta(hours=2)).isoformat()},
            {"ts": (now - timedelta(minutes=45)).isoformat()},
            {"ts": (now - timedelta(minutes=31)).isoformat()},
            {"ts": (now - timedelta(minutes=1)).isoformat()},
        ]
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        mature_before_ms = int((now - timedelta(minutes=30)).timestamp() * 1000)

        assert count_mature_new_liquidations(p, 1, mature_before_ms) == 2


# ----------------------------------------------------------------------------
# load_baseline / save_baseline
# ----------------------------------------------------------------------------

class TestBaselineIO:
    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        # Force a unique path that doesn't exist
        from src.strategy import rebuild_trigger as rt
        fake = tmp_path / "missing.json"
        assert rt.load_baseline(fake) == {}

    def test_load_corrupt_returns_empty(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        fake = tmp_path / "baseline.json"
        fake.write_text("{not valid json")
        assert rt.load_baseline(fake) == {}

    def test_save_atomic_creates_parent(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        fake = tmp_path / "nested" / "baseline.json"
        rt.save_baseline({"x": 1}, fake)
        assert fake.exists()
        assert json.loads(fake.read_text()) == {"x": 1}

    def test_save_overwrites_existing(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        fake = tmp_path / "baseline.json"
        rt.save_baseline({"x": 1}, fake)
        rt.save_baseline({"x": 2}, fake)
        assert json.loads(fake.read_text()) == {"x": 2}

    def test_save_leaves_no_tmp(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        fake = tmp_path / "baseline.json"
        rt.save_baseline({"x": 1}, fake)
        assert not fake.with_suffix(fake.suffix + ".tmp").exists()


# ----------------------------------------------------------------------------
# check_should_rebuild
# ----------------------------------------------------------------------------

def _write_liquidations(path: Path, n: int, last_dt: datetime) -> None:
    """Write n liquidations, the last one at last_dt."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n - 1):
            f.write(
                f'{{"ts": "{(last_dt - timedelta(hours=2, minutes=i)).isoformat()}", '
                f'"symbol": "BTC", "side": "A", "total_notional": 100}}\n'
            )
        f.write(
            f'{{"ts": "{last_dt.isoformat()}", '
            f'"symbol": "BTC", "side": "A", "total_notional": 100}}\n'
        )


class TestCheckShouldRebuild:
    def test_no_baseline_holds(self, tmp_path, monkeypatch):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        _write_liquidations(liq, 200, datetime(2026, 8, 2, 19, 0, tzinfo=UTC))
        now = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert not should_fire
        assert "no baseline yet" in info["reasons"][0]
        assert info["current_liquidation_count"] == 200

    def test_hold_when_too_few_new_rows(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
        _write_liquidations(liq, 100, last_dt)
        # Baseline says 95 liquidations, 70 min ago
        rt.save_baseline({
            "liquidation_count": 95,
            "last_rebuild_ts": (datetime(2026, 8, 2, 19, 50, tzinfo=UTC)).isoformat(),
            "last_liquidation_ts": last_dt.isoformat(),
        }, base)
        now = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert not should_fire
        assert info["new_rows"] == 5
        assert any("new_rows" in r for r in info["reasons"])

    def test_fire_when_latest_liq_recent_but_enough_new_rows_are_mature(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        now = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
        with liq.open("w", encoding="utf-8") as f:
            for i in range(50):
                f.write(json.dumps({"ts": (now - timedelta(hours=2, minutes=i)).isoformat()}) + "\n")
            for i in range(150):
                f.write(json.dumps({"ts": (now - timedelta(minutes=45, seconds=i)).isoformat()}) + "\n")
            f.write(json.dumps({"ts": (now - timedelta(minutes=1)).isoformat()}) + "\n")
        rt.save_baseline({
            "liquidation_count": 50,
            "last_rebuild_ts": (datetime(2026, 8, 2, 18, 0, tzinfo=UTC)).isoformat(),
        }, base)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert should_fire
        assert info["new_rows"] == 151
        assert info["mature_new_rows"] == 150
        assert info["last_liq_age_min"] < THRESHOLD_LAST_LIQ_AGE_MIN
        assert info["reasons"] == []

    def test_hold_when_rebuild_too_recent(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)  # 2h ago
        _write_liquidations(liq, 200, last_dt)
        rt.save_baseline({
            "liquidation_count": 50,
            "last_rebuild_ts": (datetime(2026, 8, 2, 20, 30, tzinfo=UTC)).isoformat(),  # 30 min ago
            "last_liquidation_ts": last_dt.isoformat(),
        }, base)
        now = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert not should_fire
        assert any("last_rebuild_age" in r for r in info["reasons"])

    def test_fire_when_all_conditions_met(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)  # 2h ago
        _write_liquidations(liq, 200, last_dt)
        rt.save_baseline({
            "liquidation_count": 50,
            "last_rebuild_ts": (datetime(2026, 8, 2, 18, 0, tzinfo=UTC)).isoformat(),  # 3h ago
            "last_liquidation_ts": last_dt.isoformat(),
        }, base)
        now = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert should_fire
        assert info["new_rows"] == 150
        assert not info["daily_fallback"]
        # On FIRE, reasons is empty (no HOLD reason); CLI helper renders
        # "all conditions met" when reasons is empty.
        assert info["reasons"] == []

    def test_daily_fallback_fires(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        # Even with brand-new liquidations (1 min ago), the daily window
        # at 00:15 PT should fire.
        now = datetime(2026, 8, 3, 7, 20, tzinfo=UTC)  # 00:20 PT on 2026-08-03
        last_dt = datetime(2026, 8, 3, 6, 50, tzinfo=UTC)  # 30 min ago
        _write_liquidations(liq, 10, last_dt)
        rt.save_baseline({}, base)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert should_fire
        assert info["daily_fallback"] is True

    def test_daily_fallback_does_not_double_fire(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)
        _write_liquidations(liq, 10, last_dt)
        rt.save_baseline({
            "last_daily_fallback_date": "2026-08-03",  # already fired today
        }, base)
        now = datetime(2026, 8, 3, 7, 20, tzinfo=UTC)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert not should_fire
        # Falls through to standard check
        assert info["daily_fallback"] is False

    def test_daily_fallback_window_excludes_after(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
        _write_liquidations(liq, 10, last_dt)
        rt.save_baseline({}, base)
        # 00:50 PT = 07:50 UTC, AFTER the 30-min window
        now = datetime(2026, 8, 3, 7, 50, tzinfo=UTC)
        should_fire, info = rt.check_should_rebuild(
            now_utc=now, liquidations_path=liq, baseline_path=base,
        )
        assert not should_fire
        assert info["daily_fallback"] is False


# ----------------------------------------------------------------------------
# update_baseline
# ----------------------------------------------------------------------------

class TestUpdateBaseline:
    def test_updates_count_and_timestamps(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
        _write_liquidations(liq, 5, last_dt)
        now = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
        payload = rt.update_baseline(
            liquidations_path=liq,
            baseline_path=base,
            now_utc=now,
        )
        assert payload["liquidation_count"] == 5
        assert payload["last_rebuild_ts"] == now.isoformat()
        assert payload["last_liquidation_ts"] is not None
        # Sanity: round-trip the last_liquidation_ts
        rt_dt = datetime.fromisoformat(payload["last_liquidation_ts"])
        assert rt_dt == last_dt

    def test_preserves_existing_fields(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
        _write_liquidations(liq, 5, last_dt)
        rt.save_baseline({"some_extra": "value"}, base)
        payload = rt.update_baseline(
            liquidations_path=liq,
            baseline_path=base,
            now_utc=datetime(2026, 8, 2, 21, 0, tzinfo=UTC),
        )
        assert payload["some_extra"] == "value"
        assert payload["liquidation_count"] == 5

    def test_daily_fallback_marks_today(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)  # 00:00 PT
        _write_liquidations(liq, 5, last_dt)
        # 00:20 PT = 07:20 UTC 2026-08-03
        now = datetime(2026, 8, 3, 7, 20, tzinfo=UTC)
        payload = rt.update_baseline(
            liquidations_path=liq,
            baseline_path=base,
            now_utc=now,
        )
        assert payload["last_daily_fallback_date"] == "2026-08-03"

    def test_no_daily_mark_outside_window(self, tmp_path):
        from src.strategy import rebuild_trigger as rt
        liq = tmp_path / "liq.jsonl"
        base = tmp_path / "base.json"
        last_dt = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
        _write_liquidations(liq, 5, last_dt)
        # 12:00 PT
        now = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
        payload = rt.update_baseline(
            liquidations_path=liq,
            baseline_path=base,
            now_utc=now,
        )
        assert "last_daily_fallback_date" not in payload

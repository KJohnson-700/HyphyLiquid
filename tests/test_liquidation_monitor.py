"""Tests for the liquidation monitor's _scan_once helper.

Regression test: the live monitor must NOT crash on partial / corrupt
JSON lines in the trade file (partial flush from upstream WS writer).
"""
import json
from pathlib import Path

import pytest

from scripts.liquidation_monitor import _scan_once, _symbol_from_trade_path
from src.strategy.liquidation_detector import LiquidationDetector


def _write_trade(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _good_trade_line(tid: int, side: str, px: str, sz: str, time_ms: int) -> str:
    return json.dumps({
        "key": str(tid),
        "trade": {
            "coin": "BTC",
            "side": side,
            "px": px,
            "sz": sz,
            "time": time_ms,
            "tid": tid,
        },
    })


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Point the monitor's offset state file into a temp dir."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    fake_state = tmp_path / "data" / ".liquidation_monitor_state.json"
    import scripts.liquidation_monitor as mod
    monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
    return fake_state


def test_scan_once_skips_bad_json_lines(tmp_path, state_file, capsys) -> None:
    """A '}}' line in the middle of a trade file must not crash the scan."""
    trade_dir = tmp_path / "data" / "trades"
    log_path = tmp_path / "liq.jsonl"
    trade_file = trade_dir / "btc_2026-08-02.jsonl"
    _write_trade(trade_file, [
        _good_trade_line(1, "A", "60000", "10.0", 1_700_000_000_000),
        "}",                                       # partial flush from writer
        "}}",                                      # another partial
        "",                                        # blank line
        _good_trade_line(2, "A", "60000", "10.0", 1_700_000_000_000),
    ])

    detector = LiquidationDetector(single_trade_min=500_000, burst_total_min=1_000_000)
    seen: set = set()
    new_trades, new_events = _scan_once(detector, seen, trade_dir, log_path)

    # Both good trades fed into the detector
    assert new_trades == 2
    # Each is a $600k single fill, which clears single_trade_min, so 2 events
    assert new_events == 2
    out = capsys.readouterr().out
    assert "skipping bad json" in out
    # Bad lines did not pollute seen_tids
    assert seen == {"1", "2"}


def test_scan_once_persists_offset(tmp_path, state_file) -> None:
    """After scan, the state file records the file size as new offset."""
    trade_dir = tmp_path / "data" / "trades"
    log_path = tmp_path / "liq.jsonl"
    trade_file = trade_dir / "btc_2026-08-02.jsonl"
    _write_trade(trade_file, [
        _good_trade_line(1, "A", "60000", "10.0", 1_700_000_000_000),
    ])

    detector = LiquidationDetector()
    _scan_once(detector, set(), trade_dir, log_path)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    saved_offset = state[str(trade_file)]
    assert saved_offset == trade_file.stat().st_size
    # Re-running should find 0 new trades (offset matches file size)
    new_trades, _ = _scan_once(detector, set(), trade_dir, log_path)
    assert new_trades == 0


def test_scan_once_holds_offset_for_partial_line(tmp_path, state_file) -> None:
    """A trailing partial line (no newline) must be retried on next scan,
    not skipped and not consumed. This is the writer-flush race fix."""
    trade_dir = tmp_path / "data" / "trades"
    log_path = tmp_path / "liq.jsonl"
    trade_file = trade_dir / "btc_2026-08-02.jsonl"
    # First, write a complete good line
    trade_file.parent.mkdir(parents=True, exist_ok=True)
    good = _good_trade_line(1, "A", "60000", "10.0", 1_700_000_000_000) + "\n"
    partial = '{"key": "2", "trade": {"coin": "BTC", "side": "A"'  # truncated, no newline
    trade_file.write_bytes(good.encode("utf-8") + partial.encode("utf-8"))

    detector = LiquidationDetector(single_trade_min=500_000, burst_total_min=1_000_000)
    seen: set = set()
    new_trades, new_events = _scan_once(detector, seen, trade_dir, log_path)

    # The complete line was processed; the partial one was held for retry
    assert new_trades == 1
    assert new_events == 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    saved_offset = state[str(trade_file)]
    # The offset must be BEFORE the partial line, not past it
    assert saved_offset == len(good)
    # Re-running should find 0 new complete trades (still partial)
    new_trades_2, _ = _scan_once(detector, set(), trade_dir, log_path)
    assert new_trades_2 == 0

    # Now the writer finishes the line
    rest = ', "px": "60000", "sz": "10.0", "time": 1700000000001, "tid": 2}}\n'
    with trade_file.open("a", encoding="utf-8") as f:
        f.write(rest)

    new_trades_3, new_events_3 = _scan_once(detector, set(), trade_dir, log_path)
    assert new_trades_3 == 1
    assert new_events_3 == 1


def test_symbol_from_trade_path_handles_hip3_safe_filenames() -> None:
    assert _symbol_from_trade_path(Path("xyz_gold_2026-08-04.jsonl")) == "xyz:GOLD"
    assert _symbol_from_trade_path(Path("xyz_silver_2026-08-04.jsonl")) == "xyz:SILVER"
    assert _symbol_from_trade_path(Path("btc_2026-08-04.jsonl")) == "BTC"

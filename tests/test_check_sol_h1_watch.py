"""Unit tests for check_sol_h1_watch.py.

All math is tested against hand-calculated expected values, not against
the live data.

Run from repo root:
    python -m pytest tests/test_check_sol_h1_watch.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.check_sol_h1_watch import (  # noqa: E402
    N_CONSECUTIVE_CYCLES,
    PAPER_SIM_MIN_DECISIONS,
    WatchCycleRecord,
    _btc_observation,
    _hype_observation,
    _load_prior_watch_log,
    _paper_sim_status,
    _pf_from_trades,
    _verdict_to_dict,
)


# ----------------------------- _pf_from_trades --------------------------- #


def test_pf_from_trades_basic():
    rows = [{"pnl": 0.5}, {"pnl": 0.3}, {"pnl": -0.4}]
    # gross_profit = 0.8, gross_loss = 0.4 -> PF = 2.0
    assert _pf_from_trades(rows) == pytest.approx(2.0)


def test_pf_from_trades_all_wins():
    rows = [{"pnl": 0.1}, {"pnl": 0.2}]
    assert _pf_from_trades(rows) == float("inf")


def test_pf_from_trades_all_losses():
    rows = [{"pnl": -0.1}, {"pnl": -0.2}]
    # gross_loss > 0, gross_profit = 0 -> 0.0
    assert _pf_from_trades(rows) == 0.0


def test_pf_from_trades_empty():
    assert _pf_from_trades([]) is None


def test_pf_from_trades_missing_pnl():
    # skip rows with no recognized pnl field
    rows = [{"foo": "bar"}, {"pnl": 0.1}, {"baz": 1}]
    assert _pf_from_trades(rows) == float("inf")  # only one win, no losses


def test_pf_from_trades_falls_back_to_net_return_pct():
    # Accepts net_return_pct as a fallback field name
    rows = [{"net_return_pct": 0.3}, {"net_return_pct": -0.1}]
    # gross_p = 0.3, gross_l = 0.1 -> 3.0
    assert _pf_from_trades(rows) == pytest.approx(3.0)


def test_pf_from_trades_falls_back_to_return_pct():
    # Accepts return_pct as a fallback field name (lane backtest output)
    rows = [{"return_pct": 0.5}, {"return_pct": -0.2}]
    # gross_p = 0.5, gross_l = 0.2 -> 2.5
    assert _pf_from_trades(rows) == pytest.approx(2.5)


# ----------------------------- _verdict_to_dict --------------------------- #


def test_verdict_to_dict_basic():
    from scripts.run_relative_value_dislocation import PromotionVerdict
    v = PromotionVerdict(
        symbol="SOL", playbook="H1", horizon_minutes=30, n=10,
        win_rate=0.5, avg_pnl_pct=0.1, median_pnl_pct=0.05, pf=1.6,
        top_win_share=0.2, passed=True, reason="all gates met",
    )
    d = _verdict_to_dict(v)
    assert d["n"] == 10
    assert d["passed"] is True
    assert d["reason"] == "all gates met"
    assert "extra" not in d  # extra is popped


def test_verdict_to_dict_none():
    d = _verdict_to_dict(None)
    assert d["n"] == 0
    assert d["passed"] is False
    assert d["reason"] == "no events"


# ----------------------------- _paper_sim_status -------------------------- #


def test_paper_sim_status_no_file(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "PAPER_DECISIONS_DIR", tmp_path)
    s = _paper_sim_status()
    assert s["wired"] is False
    assert s["decisions_today"] == 0
    assert s["status"] == "not_wired"


def test_paper_sim_status_file_no_h1_tag(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "PAPER_DECISIONS_DIR", tmp_path)
    today = watch.datetime.now(watch.timezone.utc).strftime("%Y%m%d")
    p = tmp_path / f"paper_decisions_{today}.jsonl"
    p.write_text(
        json.dumps({"symbol": "SOL", "side": "B"}) + "\n"
        + json.dumps({"symbol": "ETH", "side": "A"}) + "\n"
    )
    s = _paper_sim_status()
    assert s["decisions_today"] == 2
    assert s["h1_decisions_today"] == 0
    assert s["status"] == "not_wired"


def test_paper_sim_status_partial_h1(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "PAPER_DECISIONS_DIR", tmp_path)
    today = watch.datetime.now(watch.timezone.utc).strftime("%Y%m%d")
    p = tmp_path / f"paper_decisions_{today}.jsonl"
    p.write_text(
        json.dumps({"playbook": "sol_h1", "symbol": "SOL"}) + "\n"
        + json.dumps({"playbook": "sol_h1", "symbol": "SOL"}) + "\n"
    )
    s = _paper_sim_status()
    assert s["h1_decisions_today"] == 2
    assert s["status"] == "partial"


def test_paper_sim_status_ready(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "PAPER_DECISIONS_DIR", tmp_path)
    today = watch.datetime.now(watch.timezone.utc).strftime("%Y%m%d")
    p = tmp_path / f"paper_decisions_{today}.jsonl"
    # PAPER_SIM_MIN_DECISIONS + 1 h1-tagged entries
    lines = "\n".join(
        json.dumps({"playbook": "sol_h1", "n": i}) for i in range(PAPER_SIM_MIN_DECISIONS + 1)
    )
    p.write_text(lines + "\n")
    s = _paper_sim_status()
    assert s["h1_decisions_today"] == PAPER_SIM_MIN_DECISIONS + 1
    assert s["status"] == "ready"
    assert s["wired"] is True


# ----------------------------- WatchCycleRecord -------------------------- #


def test_watch_cycle_record_round_trip():
    rec = WatchCycleRecord(
        cycle_ts_utc="2026-08-05T00:00:00+00:00",
        cascade_count=100,
        cascade_count_delta=5,
        sol_total_events=80,
        sol_h1_30m={"n": 30, "passed": True},
        sol_h1_60m={"n": 30, "passed": True},
        btc_observation={"btc_b_total_n": 54, "btc_b_pf": 1.45},
        hype_observation={"hype_b_n": 15, "hype_b_pf": 1.34},
        paper_sim={"wired": False, "status": "not_wired"},
        consecutive_passes=2,
        cumulative_passes=2,
        status="watch-pending-paper",
        decision_rule={"cycles_required": 2},
    )
    d = rec.__dict__
    j = json.dumps(d)
    loaded = json.loads(j)
    assert loaded["cascade_count"] == 100
    assert loaded["status"] == "watch-pending-paper"


# ----------------------------- _load_prior_watch_log --------------------- #


def test_load_prior_watch_log_no_file(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "WATCH_LOG_PATH", tmp_path / "no_such_file.jsonl")
    assert _load_prior_watch_log() == []


def test_load_prior_watch_log_with_entries(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    log = tmp_path / "log.jsonl"
    log.write_text(
        json.dumps({"cycle_ts_utc": "t1", "consecutive_passes": 0}) + "\n"
        + "not-json-line\n"
        + json.dumps({"cycle_ts_utc": "t2", "consecutive_passes": 1}) + "\n"
    )
    monkeypatch.setattr(watch, "WATCH_LOG_PATH", log)
    rows = _load_prior_watch_log()
    assert len(rows) == 2
    assert rows[0]["cycle_ts_utc"] == "t1"
    assert rows[1]["consecutive_passes"] == 1


# ----------------------------- _btc_observation -------------------------- #


def test_btc_observation_no_files(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "BTC_TRAILING_SWEEP_PATH", tmp_path / "no_trail.json")
    monkeypatch.setattr(watch, "BTC_B_LANE_TRADES_PATH", tmp_path / "no_lane.jsonl")
    obs = _btc_observation()
    assert obs["btc_b_total_n"] is None
    assert obs["btc_b_pf"] is None
    assert obs["btc_b_trailing_n"] is None


def test_btc_observation_with_files(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    trailing = tmp_path / "trail.json"
    trailing.write_text(json.dumps({
        "rows": [
            {"n": 30, "profit_factor": 1.2},
            {"n": 50, "profit_factor": 1.6},
        ]
    }))
    lane = tmp_path / "lane.jsonl"
    lane.write_text(
        json.dumps({"pnl": 0.3}) + "\n"
        + json.dumps({"pnl": 0.2}) + "\n"
        + json.dumps({"pnl": -0.1}) + "\n"
    )
    monkeypatch.setattr(watch, "BTC_TRAILING_SWEEP_PATH", trailing)
    monkeypatch.setattr(watch, "BTC_B_LANE_TRADES_PATH", lane)
    obs = _btc_observation()
    assert obs["btc_b_trailing_n"] == 50
    assert obs["btc_b_trailing_best_pf"] == 1.6
    assert obs["btc_b_total_n"] == 3
    # gross_p = 0.5, gross_l = 0.1 -> PF = 5.0
    assert obs["btc_b_pf"] == pytest.approx(5.0)


# ----------------------------- _hype_observation ------------------------- #


def test_hype_observation_no_file(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    monkeypatch.setattr(watch, "LANE_HYPE_B_PATH", tmp_path / "no_hype.jsonl")
    assert _hype_observation() == {"hype_b_n": None, "hype_b_pf": None}


def test_hype_observation_with_trades(tmp_path, monkeypatch):
    import scripts.check_sol_h1_watch as watch
    p = tmp_path / "hype.jsonl"
    p.write_text(
        json.dumps({"pnl": 0.1}) + "\n"
        + json.dumps({"pnl": 0.1}) + "\n"
        + json.dumps({"pnl": -0.2}) + "\n"
    )
    monkeypatch.setattr(watch, "LANE_HYPE_B_PATH", p)
    obs = _hype_observation()
    assert obs["hype_b_n"] == 3
    # gross_p = 0.2, gross_l = 0.2 -> PF = 1.0
    assert obs["hype_b_pf"] == pytest.approx(1.0)


# ----------------------------- constants -------------------------------- #


def test_default_cycles_required():
    assert N_CONSECUTIVE_CYCLES == 2


def test_default_paper_sim_min():
    assert PAPER_SIM_MIN_DECISIONS == 5

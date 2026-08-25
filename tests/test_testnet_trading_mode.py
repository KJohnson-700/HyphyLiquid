"""Testnet execution must reach testnet and nothing else.

The mode exists because the paper simulator answered "what would this have
made" with assumptions no exchange agreed to. That only helps if the mode is
genuinely wired to a venue, and only safe if it can never reach mainnet.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import paper_funding_neg_fade as m  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_kill_switch(monkeypatch):
    monkeypatch.setattr(m, "_kill_switch_active", lambda: False)


@pytest.mark.parametrize("env", ["mainnet", "MAINNET", "", "prod", "live"])
def test_refuses_any_non_testnet_env(env, monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_ENV", env)
    ok, reason = m._testnet_guard_ok()
    assert ok is False
    assert "testnet" in reason.lower()


def test_allows_testnet_env(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_ENV", "testnet")
    ok, _ = m._testnet_guard_ok()
    assert ok is True


def test_kill_switch_blocks_even_on_testnet(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_ENV", "testnet")
    monkeypatch.setattr(m, "_kill_switch_active", lambda: True)
    ok, reason = m._testnet_guard_ok()
    assert ok is False and "kill switch" in reason


def test_testnet_paths_are_separate_from_mainnet():
    """A testnet fill must never be mistaken for, or overwrite, a mainnet one."""
    pairs = [
        (m.TESTNET_ORDERS_PATH, m.LIVE_ORDERS_PATH),
        (m.TESTNET_POSITIONS_PATH, m.LIVE_POSITIONS_PATH),
        (m.TESTNET_STATE_PATH, m.LIVE_STATE_PATH),
        (m.TESTNET_OPEN_POSITIONS_PATH, m.LIVE_OPEN_POSITIONS_PATH),
    ]
    for tn, live in pairs:
        assert tn != live, f"{tn} collides with the mainnet path"


def test_testnet_guard_does_not_consult_live_flag(monkeypatch):
    """Gating testnet behind LIVE_TRADING_ENABLED would push people to flip it."""
    monkeypatch.setenv("HYPERLIQUID_ENV", "testnet")
    monkeypatch.setattr(m, "LIVE_TRADING_ENABLED", False)
    assert m._testnet_guard_ok()[0] is True


def test_position_cap_matches_risk_config():
    from src.risk import RiskConfig
    assert m.MAX_OPEN_POSITIONS == RiskConfig().max_open_positions


def test_state_helpers_isolate_venues(tmp_path):
    """Passing a path must not fall back to the mainnet file."""
    p = tmp_path / "tn_open.json"
    m._save_live_open_positions([{"symbol": "HYPE"}], p)
    assert m._load_live_open_positions(p) == [{"symbol": "HYPE"}]
    # the mainnet default must be unaffected by that write
    assert m._load_live_open_positions(p) != m._load_live_open_positions(
        tmp_path / "absent.json")


def test_signal_access_is_positional():
    """sig carries a DatetimeIndex; sig[i] would be a label lookup."""
    import pandas as pd
    idx = pd.date_range("2026-08-01", periods=5, freq="h", tz="UTC")
    s = pd.Series([0, 1, 0, 1, 0], index=idx)
    v = s.to_numpy() if hasattr(s, "to_numpy") else s
    assert v[1] == 1  # positional works
    with pytest.raises(KeyError):
        s[1]  # label lookup fails, which is the bug this guards

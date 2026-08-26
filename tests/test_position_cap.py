"""The paper record must not contain trades live would have rejected."""
import pytest

from src.strategy.position_cap import BLOCKED_REASON, apply_position_cap, cap_summary


def _t(sym, entry, exit_, pnl=1.0):
    return {"symbol": sym, "entry_ts": entry, "exit_ts": exit_, "net_pnl_usd": pnl}


def test_fourth_concurrent_entry_is_blocked():
    trades = [
        _t("BTC", "2026-08-18 01:00:00", "2026-08-18 09:00:00"),
        _t("ETH", "2026-08-18 02:00:00", "2026-08-18 09:00:00"),
        _t("SOL", "2026-08-18 03:00:00", "2026-08-18 09:00:00"),
        _t("HYPE", "2026-08-18 04:00:00", "2026-08-18 09:00:00"),
    ]
    got = apply_position_cap(trades, max_open=3)
    assert [t["admitted"] for t in got] == [True, True, True, False]
    assert got[-1]["blocked_by"] == BLOCKED_REASON


def test_slot_frees_when_a_position_closes():
    trades = [
        _t("BTC", "2026-08-18 01:00:00", "2026-08-18 02:00:00"),
        _t("ETH", "2026-08-18 01:00:00", "2026-08-18 09:00:00"),
        _t("SOL", "2026-08-18 01:00:00", "2026-08-18 09:00:00"),
        _t("HYPE", "2026-08-18 03:00:00", "2026-08-18 09:00:00"),
    ]
    got = apply_position_cap(trades, max_open=3)
    # BTC closed at 02:00, so HYPE at 03:00 gets the slot
    assert got[-1]["admitted"] is True


def test_exit_exactly_at_entry_frees_the_slot():
    trades = [
        _t("BTC", "2026-08-18 01:00:00", "2026-08-18 03:00:00"),
        _t("ETH", "2026-08-18 01:00:00", "2026-08-18 09:00:00"),
        _t("SOL", "2026-08-18 01:00:00", "2026-08-18 09:00:00"),
        _t("HYPE", "2026-08-18 03:00:00", "2026-08-18 09:00:00"),
    ]
    assert apply_position_cap(trades, max_open=3)[-1]["admitted"] is True


def test_admission_is_first_come_not_best_pnl():
    """Live cannot see the future; a worse earlier trade still takes the slot."""
    trades = [
        _t("BTC", "2026-08-18 01:00:00", "2026-08-18 09:00:00", pnl=-50.0),
        _t("ETH", "2026-08-18 01:00:00", "2026-08-18 09:00:00", pnl=-50.0),
        _t("SOL", "2026-08-18 01:00:00", "2026-08-18 09:00:00", pnl=-50.0),
        _t("HYPE", "2026-08-18 02:00:00", "2026-08-18 09:00:00", pnl=+500.0),
    ]
    got = apply_position_cap(trades, max_open=3)
    assert got[-1]["admitted"] is False, "the profitable late trade must not jump the queue"


def test_input_is_not_mutated():
    trades = [_t("BTC", "2026-08-18 01:00:00", "2026-08-18 02:00:00")]
    apply_position_cap(trades, max_open=3)
    assert "admitted" not in trades[0]


def test_trades_missing_timestamps_are_kept():
    trades = [{"symbol": "BTC", "net_pnl_usd": 5.0}]
    got = apply_position_cap(trades, max_open=3)
    assert len(got) == 1 and got[0]["admitted"] is True


def test_cap_of_zero_is_rejected():
    with pytest.raises(ValueError):
        apply_position_cap([], max_open=0)


def test_summary_reports_the_cost():
    trades = [
        _t("BTC", "2026-08-18 01:00:00", "2026-08-18 09:00:00", 10.0),
        _t("ETH", "2026-08-18 01:00:00", "2026-08-18 09:00:00", 10.0),
        _t("SOL", "2026-08-18 01:00:00", "2026-08-18 09:00:00", 10.0),
        _t("HYPE", "2026-08-18 02:00:00", "2026-08-18 09:00:00", 7.0),
    ]
    s = cap_summary(apply_position_cap(trades, max_open=3))
    assert s["n_blocked"] == 1 and s["pnl_blocked"] == 7.0
    assert s["pnl_admitted"] == 30.0 and s["blocked_symbols"] == ["HYPE"]

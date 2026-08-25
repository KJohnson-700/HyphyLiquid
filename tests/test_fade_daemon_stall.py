"""A flat trade count has two causes and they must not be reported alike.

No qualifying bar means the lane is idle and correct. Qualifying bars with no
trade means the pipeline is broken -- which is exactly the state that ran for
11 hours reporting "ok" on every step. Escalating on the idle case would train
everyone to ignore the warning.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fade_paper_daemon as fpd  # noqa: E402
from paper_funding_neg_fade import NEG_THRESHOLD  # noqa: E402


def _panel(tmp_path, values):
    ts = pd.date_range("2026-08-24 00:00", periods=len(values), freq="h", tz="UTC")
    df = pd.DataFrame({"ts": ts, "symbol": "HYPE", "funding_actual": values})
    p = tmp_path / "funding_panel.csv"
    df.to_csv(p, index=False)
    return p


def test_counts_only_bars_under_threshold(tmp_path, monkeypatch):
    vals = [NEG_THRESHOLD * 2, NEG_THRESHOLD * 3, 1.25e-5, 0.0]
    monkeypatch.setattr(fpd, "FUNDING_PANEL", _panel(tmp_path, vals))
    assert fpd._qualifying_bars() == 2


def test_all_positive_funding_reads_as_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(fpd, "FUNDING_PANEL", _panel(tmp_path, [1.25e-5] * 6))
    assert fpd._qualifying_bars() == 0


def test_missing_panel_is_unknown_not_idle(tmp_path, monkeypatch):
    """-1, never 0: an unreadable panel must not be mistaken for a quiet market."""
    monkeypatch.setattr(fpd, "FUNDING_PANEL", tmp_path / "nope.csv")
    assert fpd._qualifying_bars() == -1


def test_only_traded_symbols_count(tmp_path, monkeypatch):
    ts = pd.date_range("2026-08-24 00:00", periods=2, freq="h", tz="UTC")
    df = pd.DataFrame({
        "ts": list(ts) * 2,
        "symbol": ["HYPE", "HYPE", "xyz:GOLD", "xyz:GOLD"],
        "funding_actual": [NEG_THRESHOLD * 2, 1.25e-5, NEG_THRESHOLD * 2, NEG_THRESHOLD * 2],
    })
    p = tmp_path / "funding_panel.csv"
    df.to_csv(p, index=False)
    monkeypatch.setattr(fpd, "FUNDING_PANEL", p)
    # xyz:GOLD has no calibrated policy and never trades; it must not mask a stall
    assert fpd._qualifying_bars() == 1

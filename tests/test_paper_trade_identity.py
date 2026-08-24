"""The fade paper lane re-simulates its whole panel on every daemon tick.

If a replayed trade does not keep its identity, it is appended again each
tick and n inflates -- and n is exactly what the graduation gates score. These
tests pin the identity down.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import paper_funding_neg_fade as pfnf  # noqa: E402


def test_paper_trade_id_is_deterministic():
    ts = pd.Timestamp("2026-08-17 10:00:00")
    assert pfnf.paper_trade_id("HYPE", ts) == pfnf.paper_trade_id("HYPE", ts)


def test_paper_trade_id_has_no_random_component():
    """gen_id() is random; it must never leak into the trade identity."""
    ts = pd.Timestamp("2026-08-17 10:00:00")
    ids = {pfnf.paper_trade_id("HYPE", ts) for _ in range(50)}
    assert len(ids) == 1, f"paper_trade_id is not stable: {ids}"


def test_distinct_bars_and_symbols_get_distinct_ids():
    a = pd.Timestamp("2026-08-17 10:00:00")
    b = pd.Timestamp("2026-08-17 11:00:00")
    assert pfnf.paper_trade_id("HYPE", a) != pfnf.paper_trade_id("HYPE", b)
    assert pfnf.paper_trade_id("HYPE", a) != pfnf.paper_trade_id("SOL", a)


def test_gen_id_is_still_random():
    """decision_id stays random -- the fix must not have flattened it."""
    assert len({pfnf.gen_id() for _ in range(50)}) == 50


def test_positions_file_has_no_duplicate_trades():
    """Guards the on-disk artifact the scorecard reads."""
    import json
    path = PROJECT_ROOT / "data" / "paper_funding_neg_fade_positions.jsonl"
    if not path.exists():
        pytest.skip("no positions file yet")
    seen, dups = set(), []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        key = (r.get("symbol"), r.get("entry_ts"), r.get("exit_ts"), r.get("status"))
        if key in seen:
            dups.append(key)
        seen.add(key)
    assert not dups, f"duplicate trades inflate n: {dups[:5]}"

"""The fade signal is an event, not a state.

signal_funding_neg_fade returns 1 for every bar funding sits below the
threshold. Entering on that level re-opens the same episode as soon as a trade
closes, which is acting on a condition that has been true for hours and is
already in the price. Measured on ETH, whose negative stretches are long:
level n=111 PF 1.04, edge n=88 PF 1.65.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = (REPO_ROOT / "scripts" / "paper_funding_neg_fade.py").read_text()


def test_entry_is_edge_triggered():
    """Every entry site must require the previous bar to be un-signalled."""
    sites = [m for m in re.finditer(r"sig\.iloc\[i\] == 1", SRC)]
    assert sites, "no entry site found -- did the signal check move?"
    for m in sites:
        window = SRC[m.start():m.start() + 200]
        assert "sig.iloc[i - 1] != 1" in window, (
            "an entry site is level-triggered; it will re-enter the same "
            "negative-funding episode after each close")


def test_no_bare_level_entry_remains():
    assert "if sig.iloc[i] == 1 and i + 1 < len(df):" not in SRC


def test_regime_labeller_prefers_the_panel():
    """The per-symbol *_candles_1h_90d_*.csv files covered a handful of symbols
    for 90 days; unlabelled trades became a "no_data" regime that satisfied a
    promotion gate."""
    src = (REPO_ROOT / "scripts" / "label_trade_regimes.py").read_text()
    assert "candle_panel.csv" in src
    assert src.index("_from_panel(symbol)") < src.index("_candle_file(symbol)\n        if p is None")

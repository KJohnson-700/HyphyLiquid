"""Regime labels must describe the market, not the asset's baseline volatility.

classify_candle_regime defaults to an absolute atr_pct >= 0.50 that overrides
every other label. On the 7-month panel that made the label meaningless for
volatile assets: 100% of HYPE bars and 100% of ZEC bars classified as
high_vol_cascade regardless of what the market did (ETH 83%, BTC 65%). Gate 2's
two-regime requirement was therefore unpassable for HYPE for reasons that had
nothing to do with its strategy.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT)); sys.path.insert(0, str(REPO_ROOT / "scripts"))

import label_trade_regimes as ltr  # noqa: E402


def _candles(vol_pct):
    """Synthetic bars whose ATR is roughly vol_pct% of price."""
    out, px = [], 100.0
    for i in range(300):
        rng = px * vol_pct / 100.0
        out.append({"timestamp": i, "t": i, "o": px, "c": px,
                    "h": px + rng / 2, "l": px - rng / 2, "v": 1.0})
    return out


def setup_function(_):
    ltr._atr_thresh_cache.clear()


def test_threshold_scales_with_the_asset():
    calm = ltr.high_atr_threshold("CALM", _candles(0.2))
    wild = ltr.high_atr_threshold("WILD", _candles(3.0))
    assert wild > calm, "a volatile asset must get a higher bar, not the same one"


def test_volatile_asset_is_not_permanently_cascade():
    """The bug: a 3% ATR asset cleared the absolute 0.5 threshold on every bar."""
    assert ltr.high_atr_threshold("WILD", _candles(3.0)) > 0.50


def test_falls_back_to_default_without_history():
    assert ltr.high_atr_threshold("THIN", _candles(1.0)[:40]) == 0.50


def test_threshold_is_cached_per_symbol():
    a = ltr.high_atr_threshold("X", _candles(1.0))
    b = ltr.high_atr_threshold("X", _candles(9.0))   # different data, same symbol
    assert a == b

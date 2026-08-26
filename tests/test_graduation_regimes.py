"""A regime gate must not be satisfied by trades we failed to label."""
from src.strategy.graduation import NON_REGIMES, ClosedTrade, score_lane


def _t(regime, pct=1.0):
    return ClosedTrade(symbol="HYPE", lane="l", net_return_pct=pct,
                       source="forward_paper", regime=regime)


def test_no_data_does_not_count_as_a_regime():
    trades = [_t("high_vol_cascade") for _ in range(20)] + [_t("no_data") for _ in range(2)]
    s = score_lane(trades)
    assert "no_data" not in s.regimes
    assert len(s.regimes) == 1, "2 unlabelled trades must not satisfy the 2-regime gate"


def test_real_regimes_still_count():
    trades = [_t("high_vol_cascade") for _ in range(10)] + [_t("trend_up") for _ in range(10)]
    assert len(score_lane(trades).regimes) == 2


def test_every_sentinel_is_filtered():
    for bad in NON_REGIMES:
        s = score_lane([_t("trend_up")] * 5 + [_t(bad)] * 5)
        assert len(s.regimes) == 1, f"{bad!r} leaked into the regime count"

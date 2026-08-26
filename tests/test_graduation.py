"""Graduation ladder gates. Per-lane only — never blended across assets."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.graduation import (  # noqa: E402
    CANARY_LIVE,
    FORWARD_PAPER,
    FORWARD_PAPER_PROVISIONAL,
    RESEARCH_ONLY,
    Attestations,
    ClosedTrade,
    canary_size_plan,
    score_all,
    score_lane,
)


def _trades(n, *, symbol="GOLD", lane="neutral", source="backtest",
            win=0.6, gain=2.0, loss=1.0, regime="trend"):
    out = []
    wins = int(round(n * win))
    for i in range(n):
        out.append(ClosedTrade(symbol=symbol, lane=lane,
                               net_return_pct=(gain if i < wins else -loss),
                               source=source, entry_ts=f"t{i}", regime=regime))
    return out


class TestGate1(unittest.TestCase):
    def test_clean_lane_reaches_forward_paper(self) -> None:
        s = score_lane(_trades(20), Attestations(logic_makes_market_sense=True))
        self.assertEqual(s.stage, FORWARD_PAPER)
        self.assertFalse(s.provisional)

    def test_n9_needs_best_candidate_attestation_and_is_provisional(self) -> None:
        att = Attestations(logic_makes_market_sense=True)
        s = score_lane(_trades(9), att)
        self.assertEqual(s.stage, RESEARCH_ONLY)   # no best-candidate attestation
        att.best_candidate_for_provisional = True
        s2 = score_lane(_trades(9), att)
        self.assertEqual(s2.stage, FORWARD_PAPER_PROVISIONAL)
        self.assertTrue(s2.provisional)

    def test_n3_never_promotes(self) -> None:
        s = score_lane(_trades(3), Attestations(logic_makes_market_sense=True,
                                                best_candidate_for_provisional=True))
        self.assertEqual(s.stage, RESEARCH_ONLY)

    def test_unattested_logic_blocks_promotion(self) -> None:
        s = score_lane(_trades(20))
        self.assertEqual(s.stage, RESEARCH_ONLY)
        self.assertIn("logic_makes_market_sense not attested", s.gate1_failures)

    def test_low_pf_blocks(self) -> None:
        s = score_lane(_trades(20, win=0.5, gain=1.0, loss=1.0),
                       Attestations(logic_makes_market_sense=True))
        self.assertEqual(s.stage, RESEARCH_ONLY)
        self.assertTrue(any("PF=" in f for f in s.gate1_failures))

    def test_low_win_rate_ok_when_payoff_carries(self) -> None:
        s = score_lane(_trades(20, win=0.35, gain=6.0, loss=1.0),
                       Attestations(logic_makes_market_sense=True))
        self.assertNotIn("win_rate", " ".join(s.gate1_failures))

    def test_profit_concentration_blocks(self) -> None:
        ts = _trades(19, win=1.0, gain=0.1)
        ts.append(ClosedTrade("GOLD", "neutral", 50.0, "backtest", regime="trend"))
        s = score_lane(ts, Attestations(logic_makes_market_sense=True))
        self.assertTrue(any("of profit" in f for f in s.gate1_failures))

    def test_negative_median_blocks(self) -> None:
        s = score_lane(_trades(20, win=0.3, gain=10.0, loss=1.0),
                       Attestations(logic_makes_market_sense=True))
        self.assertTrue(any("median" in f for f in s.gate1_failures))


class TestGate2(unittest.TestCase):
    ALL_ATT = dict(logic_makes_market_sense=True, hl_tick_size_verified=True,
                   hl_size_decimals_verified=True, hl_spread_liquidity_verified=True,
                   decision_path_has_tests=True, allowlist_promoted_intentionally=True,
                   drawdown_acceptable_1k_framework=True)

    def _mixed(self, n_bt, n_fwd):
        return (_trades(n_bt, source="backtest", regime="trend")
                + _trades(n_fwd, source="forward_paper", regime="chop"))

    def test_full_pass_reaches_canary(self) -> None:
        s = score_lane(self._mixed(15, 15), Attestations(**self.ALL_ATT))
        self.assertEqual(s.stage, CANARY_LIVE, s.gate2_failures)

    def test_single_regime_blocks_canary(self) -> None:
        ts = _trades(30, source="forward_paper", regime="trend")
        s = score_lane(ts, Attestations(**self.ALL_ATT))
        self.assertEqual(s.stage, FORWARD_PAPER)
        self.assertTrue(any("regimes=" in f for f in s.gate2_failures))

    def test_too_few_forward_trades_blocks_canary(self) -> None:
        s = score_lane(self._mixed(28, 4), Attestations(**self.ALL_ATT))
        self.assertNotEqual(s.stage, CANARY_LIVE)
        self.assertTrue(any("forward+live=" in f for f in s.gate2_failures))

    def test_missing_hl_verification_blocks_canary(self) -> None:
        att = dict(self.ALL_ATT); att["hl_tick_size_verified"] = False
        s = score_lane(self._mixed(15, 15), Attestations(**att))
        self.assertNotEqual(s.stage, CANARY_LIVE)
        self.assertIn("hl_tick_size_verified not attested", s.gate2_failures)


class TestNoBlending(unittest.TestCase):
    def test_score_lane_refuses_mixed_symbols(self) -> None:
        ts = _trades(10, symbol="HYPE") + _trades(10, symbol="SOL")
        with self.assertRaises(ValueError):
            score_lane(ts)

    def test_hype_success_does_not_promote_sol(self) -> None:
        ts = (_trades(30, symbol="HYPE", win=0.9, gain=5.0, source="forward_paper")
              + _trades(3, symbol="SOL", win=1.0, gain=5.0))
        att = {("HYPE", "neutral"): Attestations(logic_makes_market_sense=True)}
        scores = {s.symbol: s for s in score_all(ts, att)}
        self.assertNotEqual(scores["HYPE"].stage, RESEARCH_ONLY)
        self.assertEqual(scores["SOL"].stage, RESEARCH_ONLY)


class TestCanarySizing(unittest.TestCase):
    def test_new_asset_gets_50_dollar_canary(self) -> None:
        p = canary_size_plan("GOLD")
        self.assertEqual(p["bankroll_usd"], 50.0)
        self.assertEqual(p["risk_per_trade_usd"], [0.25, 0.50])

    def test_hype_is_the_named_exception(self) -> None:
        self.assertFalse(canary_size_plan("HYPE")["uses_canary_defaults"])


if __name__ == "__main__":
    unittest.main()

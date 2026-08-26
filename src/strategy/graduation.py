"""Per-lane promotion scoring for the graduation ladder.

research-only -> forward paper -> canary live -> scaled live

Scored per (symbol, lane). Metrics are NEVER blended across assets: a strong
BTC lane must not promote SOL, and HYPE's success must not promote anything.
Each lane carries its own scorecard.

Judgment gates ("logic makes market sense", "decision path has tests",
"tick/size/liquidity verified", "allowlist promoted intentionally") cannot be
computed from trade data. They are represented as explicit attestations that
must be supplied; absent an attestation they FAIL, never silently pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from statistics import median

RESEARCH_ONLY = "research_only"
FORWARD_PAPER_PROVISIONAL = "forward_paper_provisional"
FORWARD_PAPER = "forward_paper"
CANARY_LIVE = "canary_live"

# Gate 1
G1_N = 15
G1_N_PROVISIONAL = 9
G1_PF = 1.5
G1_WIN_RATE = 0.50
G1_MAX_PROFIT_SHARE = 0.40      # "no single trade > ~35-40% of total profit"
# Gate 2
G2_N_TOTAL = 30
G2_N_FORWARD = 15
G2_PF = 1.5
G2_MIN_REGIMES = 2

# Labels meaning "could not classify", not a market state. Counting these
# let 2 unlabelled trades satisfy the 2-regime gate.
NON_REGIMES = frozenset({"no_data", "unknown", "none", ""})
# Gate 3 — first live size for any new asset besides HYPE
CANARY_BANKROLL_USD = 50.0
CANARY_RISK_PER_TRADE_USD = (0.25, 0.50)
SCALE_MIN_LIVE_TRADES = 10
SCALE_MIN_PF = 1.5


@dataclass(frozen=True)
class ClosedTrade:
    """One closed trade, net of estimated fees/slippage."""

    symbol: str
    lane: str
    net_return_pct: float
    source: str            # "backtest" | "forward_paper" | "live"
    entry_ts: str = ""
    regime: str = ""       # regime label; blank counts as unknown


@dataclass
class Attestations:
    """Human-verified gates. Default False — absence is a failure, not a pass."""

    logic_makes_market_sense: bool = False
    hl_tick_size_verified: bool = False
    hl_size_decimals_verified: bool = False
    hl_spread_liquidity_verified: bool = False
    decision_path_has_tests: bool = False
    allowlist_promoted_intentionally: bool = False
    drawdown_acceptable_1k_framework: bool = False
    best_candidate_for_provisional: bool = False


@dataclass
class LaneScore:
    symbol: str
    lane: str
    n_total: int
    n_backtest: int
    n_forward: int
    n_live: int
    profit_factor: float | None
    win_rate: float | None
    median_return_pct: float | None
    payoff_ratio: float | None
    max_single_profit_share: float | None
    regimes: tuple[str, ...]
    stage: str
    gate1_failures: list[str] = field(default_factory=list)
    gate2_failures: list[str] = field(default_factory=list)
    provisional: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["regimes"] = list(self.regimes)
        return d


def _profit_factor(returns: list[float]) -> float | None:
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    if losses == 0:
        return float("inf") if gains > 0 else None
    return gains / losses


def _payoff_ratio(returns: list[float]) -> float | None:
    wins = [r for r in returns if r > 0]
    losses = [-r for r in returns if r < 0]
    if not wins or not losses:
        return None
    return (sum(wins) / len(wins)) / (sum(losses) / len(losses))


def _max_single_profit_share(returns: list[float]) -> float | None:
    gains = [r for r in returns if r > 0]
    total = sum(gains)
    if total <= 0:
        return None
    return max(gains) / total


def score_lane(trades: list[ClosedTrade], att: Attestations | None = None) -> LaneScore:
    """Score one lane. All trades must share the same (symbol, lane)."""
    if not trades:
        raise ValueError("score_lane requires at least one trade")
    symbols = {t.symbol for t in trades}
    lanes = {t.lane for t in trades}
    if len(symbols) != 1 or len(lanes) != 1:
        raise ValueError(f"metrics must not be blended across lanes: {symbols} {lanes}")
    att = att or Attestations()

    rets = [t.net_return_pct for t in trades]
    n_forward = sum(1 for t in trades if t.source == "forward_paper")
    n_live = sum(1 for t in trades if t.source == "live")
    n_backtest = sum(1 for t in trades if t.source == "backtest")
    wins = sum(1 for r in rets if r > 0)

    pf = _profit_factor(rets)
    win_rate = wins / len(rets)
    med = median(rets)
    payoff = _payoff_ratio(rets)
    share = _max_single_profit_share(rets)
    # "no_data" is the labeller's marker for a trade it could not classify --
    # missing information, not a market state. Counting it satisfied Gate 2's
    # "at least 2 regimes" on unlabelled trades, which is the exact silent pass
    # the gate exists to prevent.
    regimes = tuple(sorted({
        t.regime for t in trades
        if t.regime and t.regime not in NON_REGIMES
    }))

    s = LaneScore(
        symbol=symbols.pop(), lane=lanes.pop(), n_total=len(trades),
        n_backtest=n_backtest, n_forward=n_forward, n_live=n_live,
        profit_factor=pf, win_rate=win_rate, median_return_pct=med,
        payoff_ratio=payoff, max_single_profit_share=share, regimes=regimes,
        stage=RESEARCH_ONLY,
    )

    # ---- Gate 1: research-only -> forward paper
    f1 = s.gate1_failures
    if s.n_total < G1_N_PROVISIONAL:
        f1.append(f"n={s.n_total} < {G1_N_PROVISIONAL}")
    elif s.n_total < G1_N and not att.best_candidate_for_provisional:
        f1.append(f"n={s.n_total} < {G1_N} and not attested best candidate")
    if pf is None or pf < G1_PF:
        f1.append(f"PF={pf} < {G1_PF}")
    if win_rate < G1_WIN_RATE and not (payoff and payoff >= 2.0):
        f1.append(f"win_rate={win_rate:.0%} < {G1_WIN_RATE:.0%} and payoff not carrying it")
    if med <= 0:
        f1.append(f"median={med:.4f} not positive")
    if share is not None and share > G1_MAX_PROFIT_SHARE:
        f1.append(f"one trade is {share:.0%} of profit > {G1_MAX_PROFIT_SHARE:.0%}")
    if not att.logic_makes_market_sense:
        f1.append("logic_makes_market_sense not attested")

    if f1:
        return s
    s.stage = FORWARD_PAPER
    if s.n_total < G1_N:
        s.provisional = True
        s.stage = FORWARD_PAPER_PROVISIONAL

    # ---- Gate 2: forward paper -> canary live
    f2 = s.gate2_failures
    if s.n_total < G2_N_TOTAL:
        f2.append(f"n_total={s.n_total} < {G2_N_TOTAL}")
    if n_forward + n_live < G2_N_FORWARD:
        f2.append(f"forward+live={n_forward + n_live} < {G2_N_FORWARD}")
    if pf is None or pf < G2_PF:
        f2.append(f"PF={pf} < {G2_PF}")
    if med <= 0:
        f2.append("median not positive")
    if len(regimes) < G2_MIN_REGIMES:
        f2.append(f"regimes={len(regimes)} < {G2_MIN_REGIMES} (labelled: {regimes or 'none'})")
    for name in ("drawdown_acceptable_1k_framework", "hl_tick_size_verified",
                 "hl_size_decimals_verified", "hl_spread_liquidity_verified",
                 "decision_path_has_tests", "allowlist_promoted_intentionally"):
        if not getattr(att, name):
            f2.append(f"{name} not attested")

    if not f2:
        s.stage = CANARY_LIVE
        s.provisional = False
    return s


def score_all(trades: list[ClosedTrade],
              attestations: dict[tuple[str, str], Attestations] | None = None
              ) -> list[LaneScore]:
    """Score every (symbol, lane) independently. Never pools across assets."""
    attestations = attestations or {}
    buckets: dict[tuple[str, str], list[ClosedTrade]] = {}
    for t in trades:
        buckets.setdefault((t.symbol, t.lane), []).append(t)
    return sorted(
        (score_lane(v, attestations.get(k)) for k, v in buckets.items()),
        key=lambda s: (s.symbol, s.lane),
    )


def canary_size_plan(symbol: str) -> dict:
    """First live size. HYPE is the named exception to the $50 canary rule."""
    if symbol.upper() == "HYPE":
        return {"symbol": symbol, "uses_canary_defaults": False,
                "note": "HYPE is excluded from the $50 new-asset canary rule; size set separately"}
    lo, hi = CANARY_RISK_PER_TRADE_USD
    return {
        "symbol": symbol,
        "uses_canary_defaults": True,
        "bankroll_usd": CANARY_BANKROLL_USD,
        "risk_per_trade_usd": [lo, hi],
        "sizing": "risk-based, never max leverage",
        "scale_gate": f">= {SCALE_MIN_LIVE_TRADES} live trades at PF >= {SCALE_MIN_PF}",
    }

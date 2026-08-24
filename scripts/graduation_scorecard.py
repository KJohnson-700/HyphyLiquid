"""Score every asset/lane against the graduation ladder.

Reads closed trades from whatever sources exist and scores each (symbol, lane)
independently. Metrics are never pooled across assets: a strong BTC lane must
not promote SOL, and HYPE's result must not promote anything else.

Sources:
  data/strategy_search/detail_funding_neg_fade.json   lane=funding_neg_fade, source=backtest
  data/paper_funding_neg_fade_positions.jsonl         lane=funding_neg_fade, source=forward_paper
  data/paper_trades.jsonl                             cascade lane signals (fills only)

Judgment gates (logic makes market sense, HL tick/size/liquidity verified,
decision-path tests, intentional allowlist promotion, acceptable drawdown)
cannot be derived from returns. They are attestations supplied via
--attest SYMBOL:LANE:field. Unattested means FAIL, never a silent pass.

Usage:
  python3 scripts/graduation_scorecard.py
  python3 scripts/graduation_scorecard.py --attest HYPE:funding_neg_fade:logic_makes_market_sense
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.graduation import (  # noqa: E402
    Attestations,
    ClosedTrade,
    canary_size_plan,
    score_all,
)

FADE_DETAIL = PROJECT_ROOT / "data" / "strategy_search" / "detail_funding_neg_fade.json"
PAPER_TRADES = PROJECT_ROOT / "data" / "paper_trades.jsonl"
FADE_POSITIONS = PROJECT_ROOT / "data" / "paper_funding_neg_fade_positions.jsonl"
TRADE_REGIMES = PROJECT_ROOT / "data" / "trade_regimes.json"
OUT_JSON = PROJECT_ROOT / "data" / "graduation_scorecard.json"


def load_fade_backtest() -> list[ClosedTrade]:
    if not FADE_DETAIL.exists():
        return []
    blob = json.loads(FADE_DETAIL.read_text())
    out: list[ClosedTrade] = []
    for symbol, payload in blob.items():
        for t in payload.get("trades") or []:
            out.append(ClosedTrade(
                symbol=symbol, lane="funding_neg_fade",
                net_return_pct=float(t["net_pct"]),
                source="backtest", entry_ts=str(t.get("entry_ts", "")),
                regime="",  # unlabelled until regime tagging lands
            ))
    return out


def load_regimes() -> dict[str, str]:
    """trade id -> regime label, produced by scripts/label_trade_regimes.py."""
    if not TRADE_REGIMES.exists():
        return {}
    return {k: v.get("regime", "") for k, v in json.loads(TRADE_REGIMES.read_text()).items()}


def load_fade_forward_paper() -> list[ClosedTrade]:
    """Closed forward-paper round-trips from the funding-negative fade lane.

    net_pnl_usd is already net of costs and includes collected funding;
    convert to a percent of notional so it is comparable with backtest
    net_pct and independent of position size.
    """
    if not FADE_POSITIONS.exists():
        return []
    regimes = load_regimes()
    out: list[ClosedTrade] = []
    for line in FADE_POSITIONS.open():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("status") != "closed":
            continue
        notional = float(d.get("notional_usd") or 0.0)
        if notional <= 0:
            continue
        out.append(ClosedTrade(
            symbol=d.get("symbol", "?"), lane="funding_neg_fade",
            net_return_pct=float(d["net_pnl_usd"]) / notional * 100.0,
            source="forward_paper", entry_ts=str(d.get("entry_ts", "")),
            regime=regimes.get(d.get("paper_id") or d.get("decision_id", ""), ""),
        ))
    return out


def load_forward_paper() -> list[ClosedTrade]:
    """Closed forward-paper round-trips only. Signals without fills are not trades."""
    if not PAPER_TRADES.exists():
        return []
    out: list[ClosedTrade] = []
    for line in PAPER_TRADES.open():
        try:
            d = json.loads(line)
        except Exception:
            continue
        fills = d.get("future_fills")
        if not fills or d.get("net_pct") is None:
            continue
        out.append(ClosedTrade(
            symbol=d.get("symbol", "?"), lane=d.get("lane", "unknown"),
            net_return_pct=float(d["net_pct"]), source="forward_paper",
            entry_ts=str(d.get("signal_ts", "")), regime=str(d.get("regime", "")),
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attest", action="append", default=[],
                    metavar="SYMBOL:LANE:FIELD")
    args = ap.parse_args()

    att: dict[tuple[str, str], Attestations] = {}
    for spec in args.attest:
        try:
            sym, lane, field = spec.split(":", 2)
        except ValueError:
            print(f"bad --attest {spec!r}, want SYMBOL:LANE:FIELD", file=sys.stderr)
            return 2
        a = att.setdefault((sym, lane), Attestations())
        if not hasattr(a, field):
            print(f"unknown attestation field: {field}", file=sys.stderr)
            return 2
        setattr(a, field, True)

    trades = load_fade_backtest() + load_fade_forward_paper() + load_forward_paper()
    if not trades:
        print("no closed trades found in any source")
        return 0

    n_fwd = sum(1 for t in trades if t.source == "forward_paper")
    print(f"loaded {len(trades)} closed trades  "
          f"({len(trades) - n_fwd} backtest, {n_fwd} forward paper)\n")

    scores = score_all(trades, att)
    rows = []
    for s in scores:
        pf = "inf" if s.profit_factor == float("inf") else (
            f"{s.profit_factor:.2f}" if s.profit_factor is not None else "n/a")
        print(f"{s.symbol} / {s.lane}")
        print(f"  stage      : {s.stage.upper()}{' (provisional)' if s.provisional else ''}")
        print(f"  n          : {s.n_total}  (backtest {s.n_backtest}, fwd {s.n_forward}, live {s.n_live})")
        print(f"  PF         : {pf}    win {s.win_rate:.0%}    median {s.median_return_pct:+.4f}%")
        if s.max_single_profit_share is not None:
            print(f"  top trade  : {s.max_single_profit_share:.0%} of gross profit")
        print(f"  regimes    : {', '.join(s.regimes) if s.regimes else 'none labelled'}")
        if s.gate1_failures:
            print("  gate1 blockers:")
            for f in s.gate1_failures:
                print(f"    - {f}")
        elif s.gate2_failures:
            print("  gate2 blockers:")
            for f in s.gate2_failures:
                print(f"    - {f}")
        print()
        rows.append(s.to_dict())

    OUT_JSON.write_text(json.dumps({"lanes": rows}, indent=2))
    print(f"wrote {OUT_JSON}")
    print("\ncanary sizing for any promotion:")
    for s in scores:
        if s.stage == "canary_live":
            print(f"  {s.symbol}: {canary_size_plan(s.symbol)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

Attestations persist to data/attestations.json with who attested and when. A
promotion is a decision someone made and has to stay on the record; if it only
lived for one invocation the lane would silently fall back to RESEARCH_ONLY on
the next run and the ladder would mean nothing.

Usage:
  python3 scripts/graduation_scorecard.py
  python3 scripts/graduation_scorecard.py --attest HYPE:funding_neg_fade:logic_makes_market_sense
  python3 scripts/graduation_scorecard.py --attest SOL:funding_neg_fade:logic_makes_market_sense \
      --attested-by kslim --note "funding pays longs to hold; fade is the carry side"
  python3 scripts/graduation_scorecard.py --revoke SOL:funding_neg_fade:logic_makes_market_sense
  python3 scripts/graduation_scorecard.py --show-attestations
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.position_cap import apply_position_cap, cap_summary  # noqa: E402
from src.strategy.graduation import (  # noqa: E402
    Attestations,
    ClosedTrade,
    canary_size_plan,
    score_all,
)

ATTEST_PATH = PROJECT_ROOT / "data" / "attestations.json"
SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"


def _max_open_positions(default: int = 3) -> int:
    """Read the live cap from config so the scorecard cannot drift from risk.py."""
    try:
        import yaml
        cfg = yaml.safe_load(SETTINGS.read_text()) or {}
        for key in ("risk", "trading", "limits"):
            if isinstance(cfg.get(key), dict) and "max_open_positions" in cfg[key]:
                return int(cfg[key]["max_open_positions"])
        if "max_open_positions" in cfg:
            return int(cfg["max_open_positions"])
    except Exception:
        pass
    return default


MAX_OPEN_POSITIONS = _max_open_positions()
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

    raw = []
    for line in FADE_POSITIONS.open():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("status") == "closed":
            raw.append(d)

    # The simulator walks each symbol independently, so it can hold more lanes
    # at once than RiskManager would ever allow. Score the portfolio live would
    # actually have held, not the one the simulator imagined.
    capped = apply_position_cap(raw, MAX_OPEN_POSITIONS)
    summary = cap_summary(capped)
    if summary["n_blocked"]:
        print(f"position cap ({MAX_OPEN_POSITIONS} concurrent): "
              f"{summary['n_blocked']} of {summary['n_total']} closed trades "
              f"would have been rejected live "
              f"(${summary['pnl_blocked']:+.2f} excluded), "
              f"affecting {', '.join(summary['blocked_symbols'])}")

    out: list[ClosedTrade] = []
    for d in capped:
        if not d.get("admitted", True):
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


def load_attestations() -> dict:
    """Stored attestations: {"SYMBOL:LANE": {field: {by, at, note}}}."""
    if not ATTEST_PATH.exists():
        return {}
    try:
        return json.loads(ATTEST_PATH.read_text())
    except Exception as e:
        # Never silently treat a corrupt store as "nothing attested" -- that
        # would quietly demote every promoted lane.
        print(f"ERROR: {ATTEST_PATH} is unreadable ({e}); refusing to score with "
              f"attestations silently dropped", file=sys.stderr)
        raise SystemExit(2)


def save_attestations(store: dict) -> None:
    ATTEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ATTEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True))
    tmp.replace(ATTEST_PATH)


def to_attestation_objs(store: dict) -> dict:
    out: dict[tuple[str, str], Attestations] = {}
    for key, fields in store.items():
        try:
            sym, lane = key.split(":", 1)
        except ValueError:
            continue
        a = Attestations()
        for field in fields:
            if hasattr(a, field):
                setattr(a, field, True)
        out[(sym, lane)] = a
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attest", action="append", default=[],
                    metavar="SYMBOL:LANE:FIELD")
    ap.add_argument("--revoke", action="append", default=[],
                    metavar="SYMBOL:LANE:FIELD",
                    help="withdraw a stored attestation")
    ap.add_argument("--attested-by", default=os.getenv("USER", "unknown"),
                    help="who is making the call (default: $USER)")
    ap.add_argument("--note", default="",
                    help="why the gate is satisfied; stored with the attestation")
    ap.add_argument("--show-attestations", action="store_true",
                    help="print the stored attestations and exit")
    args = ap.parse_args()

    store = load_attestations()

    if args.show_attestations:
        if not store:
            print("no attestations recorded")
            return 0
        for key in sorted(store):
            print(key)
            for field, meta in sorted(store[key].items()):
                note = f"  -- {meta['note']}" if meta.get("note") else ""
                print(f"  {field}: {meta.get('by','?')} at {meta.get('at','?')}{note}")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dirty = False

    for spec in args.revoke:
        try:
            sym, lane, field = spec.split(":", 2)
        except ValueError:
            print(f"bad --revoke {spec!r}, want SYMBOL:LANE:FIELD", file=sys.stderr)
            return 2
        key = f"{sym}:{lane}"
        if store.get(key, {}).pop(field, None) is not None:
            print(f"revoked {field} for {key}")
            dirty = True
        else:
            print(f"{field} was not attested for {key}")

    for spec in args.attest:
        try:
            sym, lane, field = spec.split(":", 2)
        except ValueError:
            print(f"bad --attest {spec!r}, want SYMBOL:LANE:FIELD", file=sys.stderr)
            return 2
        if not hasattr(Attestations(), field):
            print(f"unknown attestation field: {field}", file=sys.stderr)
            return 2
        key = f"{sym}:{lane}"
        store.setdefault(key, {})[field] = {
            "by": args.attested_by, "at": now, "note": args.note,
        }
        print(f"attested {field} for {key}  (by {args.attested_by} at {now})")
        dirty = True

    if dirty:
        save_attestations(store)
        print(f"wrote {ATTEST_PATH}\n")

    att = to_attestation_objs(store)

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

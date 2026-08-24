"""
backtest_funding_neg_fade_detail.py

Detailed backtest of `signal_funding_neg_fade` per-symbol on HL 1h panel.
Mirrors strategy_search.py bracket logic (TP/SL) and tracks per-trade:
  - entry/exit, exit reason (tp/sl/timeout/signal_exit)
  - max adverse excursion (MAE) — deepest drawdown during trade
  - max favorable excursion (MFE) — peak runup during trade
  - hold bars
  - funding collected while in position

Output: prints per-symbol stats + writes JSON to data/strategy_search/detail_funding_neg_fade.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from strategy_search import signal_funding_neg_fade, load_hl_with_funding  # type: ignore


V1_SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
NEG_THRESHOLD = -5e-6
TP_PCT = 0.01  # 1% take profit
SL_PCT = 0.01  # 1% stop loss
MAX_HOLD_BARS = 24  # 24h
COST_BPS = 8.0


def detailed_backtest(df: pd.DataFrame) -> list[dict]:
    """Bracket-style: long on signal=1, exit on TP/SL/signal=0/max_hold."""
    sig = signal_funding_neg_fade(df, funding_col="funding_actual", neg_threshold=NEG_THRESHOLD)
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    funds = df["funding_actual"].values
    times = df.index

    trades = []
    in_trade = False
    entry_price = 0.0
    entry_idx = 0
    entry_funding = 0.0

    # Per-trade tracking
    cur_mae = 0.0  # most adverse (negative)
    cur_mfe = 0.0  # most favorable (positive)
    funding_collected = 0.0

    for i in range(len(df)):
        if not in_trade:
            if sig.iloc[i] == 1 and i + 1 < len(df):
                in_trade = True
                entry_price = closes[i]
                entry_idx = i
                entry_funding = funds[i]
                cur_mae = 0.0
                cur_mfe = 0.0
                funding_collected = 0.0
        else:
            # Update MAE/MFE on this bar
            bar_mae = (lows[i] - entry_price) / entry_price  # negative
            bar_mfe = (highs[i] - entry_price) / entry_price  # positive
            cur_mae = min(cur_mae, bar_mae)
            cur_mfe = max(cur_mfe, bar_mfe)
            # Accrue funding for this bar (we are long; if funding<0, we receive)
            if funds[i] < 0:
                funding_collected += abs(funds[i])

            hold_bars = i - entry_idx
            tp_price = entry_price * (1 + TP_PCT)
            sl_price = entry_price * (1 - SL_PCT)

            exit_now = False
            exit_reason = None
            exit_price = closes[i]

            # Check SL first (defensive — if both hit, SL is conservative)
            # NOTE: NO signal exit here — strategy_search.py also doesn't exit on signal=0.
            # Position is held until TP/SL/max_hold. Funding payments accrue the whole time.
            if lows[i] <= sl_price:
                exit_now = True
                exit_reason = "stop_loss"
                exit_price = sl_price
            elif highs[i] >= tp_price:
                exit_now = True
                exit_reason = "take_profit"
                exit_price = tp_price
            elif hold_bars >= MAX_HOLD_BARS:
                exit_now = True
                exit_reason = "max_hold"
                exit_price = closes[i]

            if exit_now:
                price_pct = (exit_price - entry_price) / entry_price
                cost_pct = COST_BPS / 10_000.0
                net_pct = (price_pct - cost_pct) * 100
                # Funding as % of notional (notional = 1 unit)
                funding_pct = funding_collected * 100
                trades.append({
                    "entry_idx": int(entry_idx),
                    "exit_idx": int(i),
                    "entry_ts": str(times[entry_idx]),
                    "exit_ts": str(times[i]),
                    "entry_price": float(entry_price),
                    "exit_price": float(exit_price),
                    "price_pct": float(price_pct * 100),
                    "funding_pct": float(funding_pct),
                    "net_pct": float(net_pct + funding_pct),  # funding adds to P&L
                    "mae_pct": float(cur_mae * 100),
                    "mfe_pct": float(cur_mfe * 100),
                    "hold_bars": int(hold_bars),
                    "exit_reason": exit_reason,
                    "entry_funding": float(entry_funding),
                })
                in_trade = False

    return trades


def score_detail(trades: list[dict]) -> dict:
    if not trades:
        return {}
    pnls = np.array([t["net_pct"] for t in trades])
    maes = np.array([t["mae_pct"] for t in trades])
    mfes = np.array([t["mfe_pct"] for t in trades])
    holds = np.array([t["hold_bars"] for t in trades])
    funds = np.array([t["funding_pct"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = (len(wins) / len(pnls) * 100) if len(pnls) else 0
    pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else 999
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {
        "n": len(trades),
        "wr": wr,
        "pf": pf,
        "med": float(np.median(pnls)),
        "mean": float(pnls.mean()),
        "net_pct_total": float(pnls.sum()),
        "funding_total_pct": float(funds.sum()),
        "mae_p10": float(np.percentile(maes, 10)),
        "mae_med": float(np.percentile(maes, 50)),
        "mae_worst": float(maes.min()),
        "mfe_p50": float(np.percentile(mfes, 50)),
        "mfe_p90": float(np.percentile(mfes, 90)),
        "mfe_max": float(mfes.max()),
        "hold_med": float(np.percentile(holds, 50)),
        "hold_p90": float(np.percentile(holds, 90)),
        "n_wins": int(len(wins)),
        "n_losses": int(len(losses)),
        "avg_win": float(wins.mean()) if len(wins) else 0,
        "avg_loss": float(losses.mean()) if len(losses) else 0,
        "exit_reasons": reasons,
    }


def main():
    out = {}
    print(f"=== detailed funding_neg_fade backtest (TP={TP_PCT*100:.1f}% SL={SL_PCT*100:.1f}% max_hold={MAX_HOLD_BARS}h) ===\n")
    for sym in V1_SYMBOLS:
        try:
            df, label = load_hl_with_funding(sym)
        except Exception as e:
            print(f"  {sym}: FAILED to load: {e}")
            continue
        n_total = len(df)
        n_neg_fund = int((df["funding_actual"] < NEG_THRESHOLD).sum())
        trades = detailed_backtest(df)
        s = score_detail(trades)
        out[sym] = {"label": label, "n_bars": n_total, "n_neg_funding_bars": n_neg_fund, **s, "trades": trades}
        print(f"--- {sym} ({label}) ---")
        print(f"  bars: {n_total}  bars with fund<={NEG_THRESHOLD:.0e}: {n_neg_fund}")
        if s:
            print(f"  trades: n={s['n']} WR={s['wr']:.1f}% PF={s['pf']:.2f} med={s['med']:+.4f}% net={s['net_pct_total']:+.4f}% funding={s['funding_total_pct']:+.4f}%")
            print(f"  MAE: med={s['mae_med']:.4f}% p10={s['mae_p10']:.4f}% worst={s['mae_worst']:.4f}%")
            print(f"  MFE: med={s['mfe_p50']:.4f}% p90={s['mfe_p90']:.4f}% max={s['mfe_max']:.4f}%")
            print(f"  hold: med={s['hold_med']:.0f}h  p90={s['hold_p90']:.0f}h")
            print(f"  wins/losses: {s['n_wins']}/{s['n_losses']}  avg_win={s['avg_win']:+.4f}%  avg_loss={s['avg_loss']:+.4f}%")
            print(f"  exits: {s['exit_reasons']}")
        print()

    out_p = PROJECT_ROOT / "data" / "strategy_search" / "detail_funding_neg_fade.json"
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(out, indent=2, default=str))
    print(f"saved -> {out_p}")


if __name__ == "__main__":
    main()

"""
HyphyLiquid — Run the backtest on real data

Loads BTC + ETH candles + funding from data/, detects cascade signals,
runs the backtester, prints honest metrics including the 50% haircut.

Run:
    .\\venv\\Scripts\\python.exe scripts\\run_backtest.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.strategy.backtest import run_backtest
from src.strategy.cascade import detect_funding_extreme, summarize_funding_extremes

DATA_DIR = PROJECT_ROOT / "data"
SYMBOLS = ["BTC", "ETH"]


def load_data():
    """Load candles and funding for all symbols."""
    candles_by_symbol = {}
    funding_by_symbol = {}
    for symbol in SYMBOLS:
        c_path = DATA_DIR / f"{symbol.lower()}_candles_1h_30d.csv"
        f_path = DATA_DIR / f"{symbol.lower()}_funding_30d.csv"
        if c_path.exists() and f_path.exists():
            candles = pd.read_csv(c_path)
            candles["timestamp"] = pd.to_datetime(
                candles["timestamp"], format="ISO8601", utc=True
            )
            funding = pd.read_csv(f_path)
            funding["timestamp"] = pd.to_datetime(
                funding["timestamp"], format="ISO8601", utc=True
            )
            candles_by_symbol[symbol] = candles
            funding_by_symbol[symbol] = funding
    return candles_by_symbol, funding_by_symbol


def print_result(label: str, result) -> None:
    print(f"--- {label} ---")
    print(f"  trades:                {result.total_trades}")
    print(f"  wins / losses:         {result.total_wins} / {result.total_losses}")
    print(f"  win rate:              {result.win_rate*100:.1f}%")
    if result.profit_factor == float("inf"):
        print(f"  profit factor:         inf (no losses)")
    else:
        print(f"  profit factor:         {result.profit_factor:.2f}")
    print(f"  avg win:               ${result.avg_win_usd:.2f}")
    print(f"  avg loss:              ${result.avg_loss_usd:.2f}")
    print(f"  total PnL:             ${result.total_pnl_usd:.2f} ({result.return_pct:+.2f}%)")
    print(f"  fees paid:             ${result.total_fees_usd:.2f}")
    print(f"  slippage cost:         ${result.total_slippage_usd:.2f}")
    print(f"  funding paid:          ${result.total_funding_paid_usd:.2f}")
    print(f"  max drawdown:          ${result.max_drawdown_usd:.2f} ({result.max_drawdown_pct:.1f}%)")
    print(f"  Sharpe (annualized):   {result.sharpe_ratio:.2f}")
    print(f"  avg bars held:         {result.avg_bars_held:.1f}")
    print(f"  bankroll:              ${result.initial_bankroll:.2f} -> ${result.final_bankroll:.2f}")
    print()
    print(f"  *** 50% DEGRADATION HAIRCUT ***")
    print(f"  real-expected PnL:     ${result.haircut_50pct_pnl:.2f}")
    print(f"  real-expected return:  {(result.haircut_50pct_pnl/result.initial_bankroll)*100:+.2f}%")
    print()


def main() -> int:
    print("HyphyLiquid - Backtest on Real Data")
    print("=" * 60)
    print()

    print("[1/3] Loading data...")
    candles_by_symbol, funding_by_symbol = load_data()
    if not candles_by_symbol:
        print("  no data found in data/ — run scripts/fetch_historical.py first")
        return 1
    for sym, df in candles_by_symbol.items():
        print(f"  {sym}: {len(df)} candles, {len(funding_by_symbol[sym])} funding events")
    print()

    print("[2/3] Detecting cascade signals...")
    all_signals = []
    for symbol in SYMBOLS:
        if symbol not in funding_by_symbol:
            continue
        funding = funding_by_symbol[symbol]
        summary = summarize_funding_extremes(funding)
        signals = detect_funding_extreme(funding)
        print(
            f"  {symbol}: {summary['count_high']} high + {summary['count_low']} low extremes "
            f"= {len(signals)} signals"
        )
        all_signals.extend(signals)
    print(f"  TOTAL: {len(all_signals)} signals")
    print()

    if not all_signals:
        print("  no signals — nothing to backtest")
        return 0

    print("[3/3] Running backtest...")
    print()

    # Per-symbol
    for symbol in SYMBOLS:
        sym_signals = [s for s in all_signals if s.symbol == symbol]
        if not sym_signals:
            continue
        result = run_backtest(
            signals=sym_signals,
            candles_by_symbol=candles_by_symbol,
            funding_by_symbol=funding_by_symbol,
            initial_bankroll=1000.0,
            risk_per_trade_pct=0.01,
            leverage=10.0,
            take_profit_atr_multiple=2.0,
            stop_loss_atr_multiple=1.0,
            max_hold_bars=24,
            slippage_bps=5.0,
            taker_fee_pct=0.00045,
            confidence_sizing=True,
        )
        print_result(f"{symbol} backtest", result)

    # Combined portfolio
    result = run_backtest(
        signals=all_signals,
        candles_by_symbol=candles_by_symbol,
        funding_by_symbol=funding_by_symbol,
        initial_bankroll=1000.0,
        risk_per_trade_pct=0.01,
        leverage=10.0,
        take_profit_atr_multiple=2.0,
        stop_loss_atr_multiple=1.0,
        max_hold_bars=24,
        slippage_bps=5.0,
        taker_fee_pct=0.00045,
        confidence_sizing=True,
    )
    print_result("COMBINED (BTC + ETH) backtest", result)

    return 0


if __name__ == "__main__":
    sys.exit(main())

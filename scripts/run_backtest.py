"""
HyphyLiquid — Run the backtest on real data

Loads BTC + ETH candles + funding from data/, detects cascade signals,
runs the backtester + parameter sweep + walk-forward validation,
prints honest metrics including the 50% haircut.

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
from src.strategy.validation import parameter_sweep, walk_forward

DATA_DIR = PROJECT_ROOT / "data"
SYMBOLS = ["BTC", "ETH"]


def _find_data(symbol: str) -> tuple[Path | None, Path | None]:
    """Prefer the longest lookback available; mainnet > testnet; fall back to 30, 7."""
    for env in ("mainnet", "testnet", ""):
        for suffix in ("90", "30", "7"):
            c = DATA_DIR / f"{symbol.lower()}_candles_1h_{suffix}d_{env}.csv" if env else \
                DATA_DIR / f"{symbol.lower()}_candles_1h_{suffix}d.csv"
            f = DATA_DIR / f"{symbol.lower()}_funding_{suffix}d_{env}.csv" if env else \
                DATA_DIR / f"{symbol.lower()}_funding_{suffix}d.csv"
            if c.exists() and f.exists():
                return c, f
    return None, None


def load_data():
    """Load candles and funding for all symbols (prefers longest lookback, mainnet > testnet)."""
    candles_by_symbol = {}
    funding_by_symbol = {}
    for symbol in SYMBOLS:
        c_path, f_path = _find_data(symbol)
        if c_path is None:
            print(f"  {symbol}: no data file found in {DATA_DIR}")
            continue
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


def print_sweep_report(report) -> None:
    print("=" * 60)
    print("PARAMETER STABILITY SWEEP")
    print("=" * 60)
    print(f"  configs tested:        {len(report.results)}")
    print(f"  PnL CoV (lower=stable): {report.pnl_coefficient_of_variation:.2f}")
    print(f"  IS STABLE:             {'YES' if report.is_stable else 'NO'}")
    print(f"  % profitable configs:  {report.pct_configs_profitable:.0f}%")
    print(f"  median win rate:       {report.median_win_rate*100:.1f}%")
    print(f"  median profit factor:  {report.median_profit_factor:.2f}")
    print(f"  median return %:       {report.median_return_pct:+.2f}%")
    print()
    if report.best_config and report.worst_config:
        print(f"  BEST CONFIG:  high_t={report.best_config.high_threshold*100:.4f}%  "
              f"low_t={report.best_config.low_threshold*100:.4f}%  "
              f"return={report.best_config.return_pct:+.2f}%  "
              f"WR={report.best_config.win_rate*100:.0f}%  "
              f"PF={report.best_config.profit_factor:.2f}")
        print(f"  WORST CONFIG: high_t={report.worst_config.high_threshold*100:.4f}%  "
              f"low_t={report.worst_config.low_threshold*100:.4f}%  "
              f"return={report.worst_config.return_pct:+.2f}%  "
              f"WR={report.worst_config.win_rate*100:.0f}%  "
              f"PF={report.worst_config.profit_factor:.2f}")
    print()


def print_walk_forward(result) -> None:
    print("=" * 60)
    print("WALK-FORWARD VALIDATION")
    print("=" * 60)
    print(f"  folds:                 {result.n_folds}")
    print(f"  avg train PnL:         ${result.avg_train_pnl:.2f}")
    print(f"  avg test PnL:          ${result.avg_test_pnl:.2f}")
    print(f"  test/train degradation: {result.test_pnl_degradation_pct:+.1f}%")
    print(f"  avg test win rate:     {result.avg_test_win_rate*100:.1f}%")
    print(f"  avg test profit factor: {result.avg_test_profit_factor:.2f}")
    print(f"  % profitable folds:    {result.pct_folds_profitable:.0f}%")
    print(f"  CONSISTENT:            {'YES' if result.consistent_across_folds else 'NO'}")
    print()
    print(f"  per-fold breakdown:")
    for f in result.folds:
        marker = "+" if f.test_pnl_usd >= 0 else "-"
        print(
            f"    fold {f.fold_id}: train_pnl=${f.train_pnl_usd:+.2f} ({f.train_return_pct:+.1f}%)  "
            f"test_pnl=${f.test_pnl_usd:+.2f} ({f.test_return_pct:+.1f}%)  "
            f"test_WR={f.test_win_rate*100:.0f}%  "
            f"[{marker}]"
        )
    print()


def main() -> int:
    print("HyphyLiquid - Backtest + Validation on Real Data")
    print("=" * 60)
    print()

    print("[1/5] Loading data...")
    candles_by_symbol, funding_by_symbol = load_data()
    if not candles_by_symbol:
        print("  no data found in data/ - run scripts/fetch_historical.py first")
        return 1
    for sym, df in candles_by_symbol.items():
        print(f"  {sym}: {len(df)} candles, {len(funding_by_symbol[sym])} funding events")
    print()

    print("[2/5] Detecting cascade signals (default thresholds)...")
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
        print("  no signals - nothing to backtest")
        return 0

    print("[3/5] Running baseline backtest (default thresholds)...")
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

    print("[4/5] Parameter stability sweep...")
    sweep = parameter_sweep(
        candles_by_symbol=candles_by_symbol,
        funding_by_symbol=funding_by_symbol,
        high_thresholds=[0.0005, 0.0010, 0.0015, 0.0020, 0.0025, 0.0030],
        low_thresholds=[-0.0010, -0.0007, -0.0005, -0.0003, -0.0001],
    )
    print_sweep_report(sweep)

    print("[5/5] Walk-forward validation (3 folds)...")
    wf = walk_forward(
        candles_by_symbol=candles_by_symbol,
        funding_by_symbol=funding_by_symbol,
        n_folds=3,
        train_frac=0.6,
        high_threshold=0.0010,
        low_threshold=-0.0005,
    )
    print_walk_forward(wf)

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
HyphyLiquid — Mainnet-scale parameter sweep.

Testnet funding is 100-200x noisier than mainnet. The thresholds tuned on
testnet (0.05%-0.30%) would never fire on mainnet. This script sweeps
mainnet-scale thresholds (0.0005%-0.003% per hour) to see if the cascade
edge exists in the real market.

Run:
    .\\venv\\Scripts\\python.exe scripts\\mainnet_sweep.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from src.strategy.cascade import summarize_funding_extremes
from src.strategy.validation import parameter_sweep

DATA_DIR = PROJECT_ROOT / "data"
SYMBOLS = ["BTC", "ETH"]


def _find_data(symbol: str) -> tuple[Path | None, Path | None]:
    """Find mainnet data files for a symbol, longest lookback first."""
    for suffix in ("90", "30", "7"):
        c = DATA_DIR / f"{symbol.lower()}_candles_1h_{suffix}d_mainnet.csv"
        f = DATA_DIR / f"{symbol.lower()}_funding_{suffix}d_mainnet.csv"
        if c.exists() and f.exists():
            return c, f
    return None, None


def main() -> int:
    print("HyphyLiquid - Mainnet-Scale Parameter Sweep")
    print("=" * 60)
    print()

    candles_by_symbol = {}
    funding_by_symbol = {}
    for symbol in SYMBOLS:
        c_path, f_path = _find_data(symbol)
        if c_path is None:
            print(f"  {symbol}: no mainnet data — run fetch with HYPERLIQUID_ENV=mainnet")
            continue
        candles = pd.read_csv(c_path)
        candles["timestamp"] = pd.to_datetime(candles["timestamp"], format="ISO8601", utc=True)
        funding = pd.read_csv(f_path)
        funding["timestamp"] = pd.to_datetime(funding["timestamp"], format="ISO8601", utc=True)
        candles_by_symbol[symbol] = candles
        funding_by_symbol[symbol] = funding
        summary = summarize_funding_extremes(funding, high_threshold=0.001, low_threshold=-0.001)
        print(
            f"  {symbol}: {len(candles)} candles, {len(funding)} funding events  "
            f"(with 0.001% thr: {summary['count_high']} high + {summary['count_low']} low)"
        )

    if not candles_by_symbol:
        print("  no data — aborting")
        return 1

    print()
    print("Running sweep at mainnet scale (high 1.0e-5 to 2.0e-5, low -1.0e-5 to -3.0e-5)...")
    print()
    sweep = parameter_sweep(
        candles_by_symbol=candles_by_symbol,
        funding_by_symbol=funding_by_symbol,
        high_thresholds=[1.0e-5, 1.2e-5, 1.4e-5, 1.6e-5, 1.8e-5, 2.0e-5],
        low_thresholds=[-1.0e-5, -1.5e-5, -2.0e-5, -2.5e-5, -3.0e-5],
    )

    print(f"  configs tested:        {len(sweep.results)}")
    print(f"  PnL CoV:               {sweep.pnl_coefficient_of_variation:.2f}")
    print(f"  IS STABLE:             {'YES' if sweep.is_stable else 'NO'}")
    print(f"  % profitable configs:  {sweep.pct_configs_profitable:.0f}%")
    print(f"  median win rate:       {sweep.median_win_rate*100:.1f}%")
    print(f"  median profit factor:  {sweep.median_profit_factor:.2f}")
    print(f"  median return %:       {sweep.median_return_pct:+.2f}%")
    print()
    if sweep.best_config and sweep.worst_config:
        print(f"  BEST:  high={sweep.best_config.high_threshold*100:.4f}%  "
              f"low={sweep.best_config.low_threshold*100:.4f}%  "
              f"return={sweep.best_config.return_pct:+.2f}%  "
              f"WR={sweep.best_config.win_rate*100:.0f}%  "
              f"PF={sweep.best_config.profit_factor:.2f}  "
              f"signals={sweep.best_config.n_signals}")
        print(f"  WORST: high={sweep.worst_config.high_threshold*100:.4f}%  "
              f"low={sweep.worst_config.low_threshold*100:.4f}%  "
              f"return={sweep.worst_config.return_pct:+.2f}%  "
              f"WR={sweep.worst_config.win_rate*100:.0f}%  "
              f"PF={sweep.worst_config.profit_factor:.2f}  "
              f"signals={sweep.worst_config.n_signals}")

    print()
    print("Per-config table (sorted by return desc):")
    print(f"  {'high%':>9}  {'low%':>9}  {'sig':>5}  {'WR%':>6}  {'PF':>7}  {'return%':>10}")
    for r in sorted(sweep.results, key=lambda x: x.return_pct, reverse=True):
        print(
            f"  {r.high_threshold*100:>8.4f}  {r.low_threshold*100:>8.4f}  "
            f"{r.n_signals:>5}  {r.win_rate*100:>5.1f}  "
            f"{r.profit_factor:>7.2f}  {r.return_pct:>+9.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

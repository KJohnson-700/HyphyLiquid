"""
HyphyLiquid — Strategy validation module

Two anti-overfitting checks per the neo_hedgefund methodology:
1. Parameter stability sweep — does the strategy work across a range of
   thresholds, or only at the specific one we picked after seeing data?
2. Walk-forward analysis — train on the first 60% of data, then test on
   the last 40% with a frozen threshold. Generalization check.

The point isn't to make the strategy look better. It's to make us
confident the strategy works for reasons other than overfit to history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.strategy.backtest import BacktestResult, run_backtest
from src.strategy.cascade import detect_funding_extreme, summarize_funding_extremes


# ---------- Parameter stability sweep ----------


@dataclass
class SweepResult:
    """Result of a single threshold combo in a parameter sweep."""

    high_threshold: float
    low_threshold: float
    n_signals: int
    win_rate: float
    profit_factor: float
    total_pnl_usd: float
    return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    haircut_50pct_pnl: float


@dataclass
class StabilityReport:
    """Aggregated output of a parameter sweep."""

    results: List[SweepResult]
    is_stable: bool  # True if performance is consistent across the grid
    pnl_coefficient_of_variation: float  # lower = more stable
    best_config: SweepResult
    worst_config: SweepResult
    median_win_rate: float
    median_profit_factor: float
    median_return_pct: float
    pct_configs_profitable: float  # % of threshold combos that produced positive PnL

    def to_dataframe(self) -> pd.DataFrame:
        rows = []
        for r in self.results:
            rows.append({
                "high_threshold": r.high_threshold,
                "low_threshold": r.low_threshold,
                "n_signals": r.n_signals,
                "win_rate": r.win_rate,
                "profit_factor": r.profit_factor,
                "total_pnl_usd": r.total_pnl_usd,
                "return_pct": r.return_pct,
                "max_drawdown_pct": r.max_drawdown_pct,
                "sharpe_ratio": r.sharpe_ratio,
                "haircut_50pct_pnl": r.haircut_50pct_pnl,
            })
        return pd.DataFrame(rows).sort_values("return_pct", ascending=False).reset_index(drop=True)


def parameter_sweep(
    candles_by_symbol: Dict[str, pd.DataFrame],
    funding_by_symbol: Dict[str, pd.DataFrame],
    high_thresholds: Optional[List[float]] = None,
    low_thresholds: Optional[List[float]] = None,
    initial_bankroll: float = 1000.0,
    risk_per_trade_pct: float = 0.01,
    leverage: float = 10.0,
    take_profit_atr_multiple: float = 2.0,
    stop_loss_atr_multiple: float = 1.0,
    max_hold_bars: int = 24,
    slippage_bps: float = 5.0,
    taker_fee_pct: float = 0.00045,
) -> StabilityReport:
    """
    Run the cascade backtest across a grid of (high_threshold, low_threshold) pairs.
    A "stable" strategy works across a range of thresholds. A "fragile" one only works
    at the specific number picked from looking at the data.

    Args:
        candles_by_symbol: {symbol: candles_df} passed to run_backtest
        funding_by_symbol: {symbol: funding_df} passed to run_backtest
        high_thresholds: list of high funding thresholds to try (default: 0.05% to 0.20%)
        low_thresholds: list of low funding thresholds to try (default: -0.10% to -0.02%)

    Returns:
        StabilityReport with all sweep results + stability assessment
    """
    if high_thresholds is None:
        high_thresholds = [0.0005, 0.0010, 0.0015, 0.0020, 0.0025, 0.0030]
    if low_thresholds is None:
        low_thresholds = [-0.0010, -0.0005, -0.0003, -0.0001]

    results: List[SweepResult] = []
    for high_t in high_thresholds:
        for low_t in low_thresholds:
            # Skip invalid combinations (e.g., high < |low| means everything is a signal)
            if high_t < abs(low_t) * 0.5:
                continue
            sweep_signals = []
            for symbol, funding in funding_by_symbol.items():
                sym_signals = detect_funding_extreme(
                    funding, high_threshold=high_t, low_threshold=low_t
                )
                # Tag coin for each signal
                for s in sym_signals:
                    s.symbol = symbol
                sweep_signals.extend(sym_signals)

            if not sweep_signals:
                continue

            backtest = run_backtest(
                signals=sweep_signals,
                candles_by_symbol=candles_by_symbol,
                funding_by_symbol=funding_by_symbol,
                initial_bankroll=initial_bankroll,
                risk_per_trade_pct=risk_per_trade_pct,
                leverage=leverage,
                take_profit_atr_multiple=take_profit_atr_multiple,
                stop_loss_atr_multiple=stop_loss_atr_multiple,
                max_hold_bars=max_hold_bars,
                slippage_bps=slippage_bps,
                taker_fee_pct=taker_fee_pct,
            )

            results.append(SweepResult(
                high_threshold=high_t,
                low_threshold=low_t,
                n_signals=backtest.total_trades,
                win_rate=backtest.win_rate,
                profit_factor=backtest.profit_factor,
                total_pnl_usd=backtest.total_pnl_usd,
                return_pct=backtest.return_pct,
                max_drawdown_pct=backtest.max_drawdown_pct,
                sharpe_ratio=backtest.sharpe_ratio,
                haircut_50pct_pnl=backtest.haircut_50pct_pnl,
            ))

    return _summarize_sweep(results)


def _summarize_sweep(results: List[SweepResult]) -> StabilityReport:
    if not results:
        # All combos filtered out (e.g., invalid thresholds). Return an empty
        # report rather than crash — callers can check `len(report.results) == 0`.
        return StabilityReport(
            results=[],
            is_stable=False,
            pnl_coefficient_of_variation=float("inf"),
            best_config=None,  # type: ignore[arg-type]
            worst_config=None,  # type: ignore[arg-type]
            median_win_rate=0.0,
            median_profit_factor=0.0,
            median_return_pct=0.0,
            pct_configs_profitable=0.0,
        )

    pnls = [r.total_pnl_usd for r in results]
    pnls_arr = np.array(pnls)
    pnl_mean = float(np.mean(pnls_arr))
    pnl_std = float(np.std(pnls_arr))
    pnl_cv = pnl_std / abs(pnl_mean) if pnl_mean != 0 else float("inf")

    # Sorted by PnL to find best/worst
    sorted_by_pnl = sorted(results, key=lambda r: r.total_pnl_usd, reverse=True)

    # Stability: low CV (consistent PnL across configs) = stable
    # Threshold: CV < 0.5 means std < 50% of mean — fairly stable
    is_stable = pnl_cv < 0.5 and pnl_mean > 0

    # % of configs that are profitable
    pct_profitable = sum(1 for r in results if r.total_pnl_usd > 0) / len(results) * 100

    return StabilityReport(
        results=results,
        is_stable=is_stable,
        pnl_coefficient_of_variation=pnl_cv,
        best_config=sorted_by_pnl[0],
        worst_config=sorted_by_pnl[-1],
        median_win_rate=float(np.median([r.win_rate for r in results])),
        median_profit_factor=float(np.median([r.profit_factor for r in results if r.profit_factor != float("inf")] or [0.0])),
        median_return_pct=float(np.median([r.return_pct for r in results])),
        pct_configs_profitable=pct_profitable,
    )


# ---------- Walk-forward analysis ----------


@dataclass
class WalkForwardFold:
    """A single train/test split result."""

    fold_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_train_signals: int
    n_test_signals: int
    train_pnl_usd: float
    test_pnl_usd: float
    train_return_pct: float
    test_return_pct: float
    test_win_rate: float
    test_profit_factor: float
    test_sharpe: float


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward result."""

    folds: List[WalkForwardFold]
    n_folds: int
    avg_train_pnl: float
    avg_test_pnl: float
    test_pnl_degradation_pct: float  # how much worse test was than train
    avg_test_win_rate: float
    avg_test_profit_factor: float
    consistent_across_folds: bool  # True if test is positive in >= 50% of folds
    pct_folds_profitable: float

    def to_dataframe(self) -> pd.DataFrame:
        rows = []
        for f in self.folds:
            rows.append({
                "fold_id": f.fold_id,
                "train_start": f.train_start,
                "train_end": f.train_end,
                "test_start": f.test_start,
                "test_end": f.test_end,
                "n_train_signals": f.n_train_signals,
                "n_test_signals": f.n_test_signals,
                "train_pnl_usd": f.train_pnl_usd,
                "test_pnl_usd": f.test_pnl_usd,
                "train_return_pct": f.train_return_pct,
                "test_return_pct": f.test_return_pct,
                "test_win_rate": f.test_win_rate,
                "test_profit_factor": f.test_profit_factor,
                "test_sharpe": f.test_sharpe,
            })
        return pd.DataFrame(rows)


def walk_forward(
    candles_by_symbol: Dict[str, pd.DataFrame],
    funding_by_symbol: Dict[str, pd.DataFrame],
    n_folds: int = 3,
    train_frac: float = 0.6,
    high_threshold: float = 0.0010,
    low_threshold: float = -0.0005,
    initial_bankroll: float = 1000.0,
    risk_per_trade_pct: float = 0.01,
    leverage: float = 10.0,
    take_profit_atr_multiple: float = 2.0,
    stop_loss_atr_multiple: float = 1.0,
    max_hold_bars: int = 24,
    slippage_bps: float = 5.0,
    taker_fee_pct: float = 0.00045,
) -> WalkForwardResult:
    """
    Walk-forward analysis. Split the time-ordered data into N sequential folds.
    For each fold: train on the first 60% (find optimal parameters / run backtest),
    then test on the next 40% with frozen parameters.

    A strategy that only works on training but not testing is overfit.
    A strategy that works on both is robust.

    Args:
        n_folds: number of train/test splits
        train_frac: fraction of each fold to use for training (default 60%)
        high_threshold, low_threshold: frozen thresholds for the backtest

    Returns:
        WalkForwardResult with per-fold metrics and aggregate stats
    """
    # Find common time range across all symbols
    all_start = max(df["timestamp"].min() for df in funding_by_symbol.values())
    all_end = min(df["timestamp"].max() for df in funding_by_symbol.values())
    total_ms = (all_end - all_start).total_seconds() * 1000
    fold_ms = total_ms / n_folds

    folds: List[WalkForwardFold] = []
    for fold_id in range(n_folds):
        fold_start = all_start + pd.Timedelta(milliseconds=fold_id * fold_ms)
        fold_end = all_start + pd.Timedelta(milliseconds=(fold_id + 1) * fold_ms)
        train_end = fold_start + pd.Timedelta(milliseconds=fold_ms * train_frac)
        # Train: [fold_start, train_end)
        # Test:  [train_end, fold_end)

        # Filter funding to train / test
        train_funding = {
            sym: df[(df["timestamp"] >= fold_start) & (df["timestamp"] < train_end)]
            for sym, df in funding_by_symbol.items()
        }
        test_funding = {
            sym: df[(df["timestamp"] >= train_end) & (df["timestamp"] < fold_end)]
            for sym, df in funding_by_symbol.items()
        }
        # Also filter candles (they need to cover both train and test periods)
        train_candles = {
            sym: df[(df["timestamp"] >= fold_start) & (df["timestamp"] < train_end)]
            for sym, df in candles_by_symbol.items()
        }
        test_candles = {
            sym: df[(df["timestamp"] >= train_end) & (df["timestamp"] < fold_end)]
            for sym, df in candles_by_symbol.items()
        }

        # Build signals
        def _build_signals(funding_dict, candles_dict):
            sigs = []
            for sym, fdf in funding_dict.items():
                if fdf.empty or sym not in candles_dict or candles_dict[sym].empty:
                    continue
                sym_sigs = detect_funding_extreme(
                    fdf, high_threshold=high_threshold, low_threshold=low_threshold
                )
                for s in sym_sigs:
                    s.symbol = sym
                sigs.extend(sym_sigs)
            return sigs

        train_signals = _build_signals(train_funding, train_candles)
        test_signals = _build_signals(test_funding, test_candles)

        # Run backtests
        train_result = run_backtest(
            signals=train_signals,
            candles_by_symbol=candles_by_symbol,  # use FULL candles for ATR lookback
            funding_by_symbol=funding_by_symbol,
            initial_bankroll=initial_bankroll,
            risk_per_trade_pct=risk_per_trade_pct,
            leverage=leverage,
            take_profit_atr_multiple=take_profit_atr_multiple,
            stop_loss_atr_multiple=stop_loss_atr_multiple,
            max_hold_bars=max_hold_bars,
            slippage_bps=slippage_bps,
            taker_fee_pct=taker_fee_pct,
        ) if train_signals else None

        test_result = run_backtest(
            signals=test_signals,
            candles_by_symbol=candles_by_symbol,
            funding_by_symbol=funding_by_symbol,
            initial_bankroll=initial_bankroll,
            risk_per_trade_pct=risk_per_trade_pct,
            leverage=leverage,
            take_profit_atr_multiple=take_profit_atr_multiple,
            stop_loss_atr_multiple=stop_loss_atr_multiple,
            max_hold_bars=max_hold_bars,
            slippage_bps=slippage_bps,
            taker_fee_pct=taker_fee_pct,
        ) if test_signals else None

        folds.append(WalkForwardFold(
            fold_id=fold_id,
            train_start=fold_start,
            train_end=train_end,
            test_start=train_end,
            test_end=fold_end,
            n_train_signals=len(train_signals),
            n_test_signals=len(test_signals),
            train_pnl_usd=train_result.total_pnl_usd if train_result else 0.0,
            test_pnl_usd=test_result.total_pnl_usd if test_result else 0.0,
            train_return_pct=train_result.return_pct if train_result else 0.0,
            test_return_pct=test_result.return_pct if test_result else 0.0,
            test_win_rate=test_result.win_rate if test_result else 0.0,
            test_profit_factor=test_result.profit_factor if test_result else 0.0,
            test_sharpe=test_result.sharpe_ratio if test_result else 0.0,
        ))

    return _summarize_walk_forward(folds)


def _summarize_walk_forward(folds: List[WalkForwardFold]) -> WalkForwardResult:
    if not folds:
        raise ValueError("No folds in walk-forward result")

    avg_train = float(np.mean([f.train_pnl_usd for f in folds]))
    avg_test = float(np.mean([f.test_pnl_usd for f in folds]))
    # Degradation: how much worse test is than train (in %)
    if avg_train > 0:
        degradation = (avg_train - avg_test) / avg_train * 100
    else:
        degradation = 0.0

    avg_wr = float(np.mean([f.test_win_rate for f in folds]))
    avg_pf_vals = [f.test_profit_factor for f in folds if f.test_profit_factor != float("inf")]
    avg_pf = float(np.mean(avg_pf_vals)) if avg_pf_vals else 0.0
    pct_profitable = sum(1 for f in folds if f.test_pnl_usd > 0) / len(folds) * 100

    # Consistent if at least half the folds are profitable
    consistent = pct_profitable >= 50.0

    return WalkForwardResult(
        folds=folds,
        n_folds=len(folds),
        avg_train_pnl=avg_train,
        avg_test_pnl=avg_test,
        test_pnl_degradation_pct=degradation,
        avg_test_win_rate=avg_wr,
        avg_test_profit_factor=avg_pf,
        consistent_across_folds=consistent,
        pct_folds_profitable=pct_profitable,
    )

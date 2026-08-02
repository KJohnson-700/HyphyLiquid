"""
HyphyLiquid - momentum cascade backtest (no funding).

Detects big 1h moves with high volume, enters FADE (against the move),
exits on TP/SL/max-hold. Sweeps thresholds and reports honest metrics.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
from src.strategy.backtest import BacktestResult, run_backtest
from src.strategy.cascade import CascadeSignal, SignalDirection

DATA_DIR = PROJECT_ROOT / "data"
SYMBOLS = ("BTC", "ETH")


def _load(symbol: str) -> pd.DataFrame:
    c = pd.read_csv(DATA_DIR / f"{symbol.lower()}_candles_1h_90d_mainnet.csv")
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)
    return c


def detect_momentum_signals(
    candles: pd.DataFrame,
    symbol: str,
    move_stdev: float = 2.0,
    vol_multiple: float = 2.0,
    return_window: int = 24,
    vol_window: int = 24,
) -> list[CascadeSignal]:
    """Return CascadeSignals for big-move bars (fade direction)."""
    c = candles.sort_values("timestamp").reset_index(drop=True).copy()
    c["ret_1h"] = c["close"].pct_change()
    c["ret_stdev"] = c["ret_1h"].rolling(return_window, min_periods=return_window).std()
    c["z_move"] = c["ret_1h"] / c["ret_stdev"]
    vol = c["volume"].rolling(vol_window, min_periods=vol_window).mean()
    c["vol_ratio"] = c["volume"] / vol
    c = c.dropna(subset=["z_move", "vol_ratio"])

    sigs: list[CascadeSignal] = []
    for _, row in c.iterrows():
        if abs(row["z_move"]) < move_stdev or row["vol_ratio"] < vol_multiple:
            continue
        # Fade: short after big UP move, long after big DOWN move
        if row["z_move"] > 0:
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.LONG
        confidence = min(1.0, abs(row["z_move"]) / 5.0)
        sigs.append(
            CascadeSignal(
                symbol=symbol,
                direction=direction,
                confidence=confidence,
                reason=f"momentum fade: z={row['z_move']:+.2f}, vol={row['vol_ratio']:.2f}x",
                funding_rate=None,
                timestamp=pd.Timestamp(row["timestamp"]),
            )
        )
    return sigs


def _load_candles_by_symbol() -> dict[str, pd.DataFrame]:
    out = {}
    for sym in SYMBOLS:
        c = _load(sym)
        out[sym] = c
    return out


def main() -> int:
    print("HyphyLiquid - Momentum Cascade Backtest (90d mainnet)")
    print("=" * 60)

    candles_by_symbol = _load_candles_by_symbol()
    for sym, c in candles_by_symbol.items():
        print(f"  {sym}: {len(c)} candles")

    # Sweep grid
    print("\nSweeping (move_stdev, vol_multiple)...")
    print(f"  {'move':>5}  {'vol':>5}  {'sym':>3}  {'n':>4}  {'WR%':>6}  {'PF':>7}  {'ret%':>9}  {'maxDD%':>7}  {'haircut%':>9}")
    results = []
    for move in (1.5, 2.0, 2.5, 3.0):
        for vol in (1.5, 2.0, 2.5, 3.0):
            for sym in SYMBOLS:
                sigs = detect_momentum_signals(
                    candles_by_symbol[sym], sym, move_stdev=move, vol_multiple=vol
                )
                if not sigs:
                    continue
                # Build single-symbol funding stub (backtest expects it; momentum doesn't use it)
                funding = candles_by_symbol[sym][["timestamp"]].copy()
                funding["coin"] = sym
                funding["funding_rate"] = 0.0
                funding["premium"] = 0.0
                res = run_backtest(
                    signals=sigs,
                    candles_by_symbol={sym: candles_by_symbol[sym]},
                    funding_by_symbol={sym: funding},
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
                wr = res.win_rate * 100
                pf = res.profit_factor if res.profit_factor != float("inf") else 99.0
                results.append({
                    "move": move, "vol": vol, "sym": sym,
                    "n": res.total_trades, "wr": wr, "pf": pf,
                    "ret": res.return_pct, "maxdd": res.max_drawdown_pct,
                    "haircut": (res.haircut_50pct_pnl / res.initial_bankroll) * 100,
                })
                print(f"  {move:>5.1f}  {vol:>5.1f}  {sym:>3}  {res.total_trades:>4}  "
                      f"{wr:>5.1f}  {pf:>6.2f}  {res.return_pct:>+8.2f}  "
                      f"{res.max_drawdown_pct:>6.1f}  {(res.haircut_50pct_pnl/res.initial_bankroll)*100:>+8.2f}")

    # Combined backtest with one config
    print("\n=== Combined BTC+ETH at (move=2.0, vol=2.0) ===")
    all_sigs = []
    for sym in SYMBOLS:
        all_sigs.extend(detect_momentum_signals(candles_by_symbol[sym], sym, 2.0, 2.0))
    all_sigs.sort(key=lambda s: s.timestamp)
    funding_stub = {
        sym: candles_by_symbol[sym][["timestamp"]].assign(coin=sym, funding_rate=0.0, premium=0.0)
        for sym in SYMBOLS
    }
    res = run_backtest(
        signals=all_sigs,
        candles_by_symbol=candles_by_symbol,
        funding_by_symbol=funding_stub,
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
    print(f"  trades: {res.total_trades}  WR: {res.win_rate*100:.1f}%  PF: {res.profit_factor:.2f}  "
          f"return: {res.return_pct:+.2f}%  maxDD: {res.max_drawdown_pct:.1f}%")
    print(f"  50% haircut: {(res.haircut_50pct_pnl/res.initial_bankroll)*100:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())

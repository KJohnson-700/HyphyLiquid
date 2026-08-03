# TP/SL Sweep Handoff - 2026-08-03

## Scope

Codex added an analysis-only TP/SL layer for lane entries. It does not alter
paper or live execution. It re-scores existing lane entries with explicit
raw-price stops and R-multiple targets so Slim can pick settings from data
instead of guessing.

## Exit Model

- Stop distance is raw price basis points, not ROE.
- Target is an R multiple of the stop distance.
- Round-trip cost haircut defaults to 8 bps.
- If a candle touches both stop and target, stop wins.
- Reports include MAE, MFE, exit reason, win rate, profit factor, average R,
  stop hit rate, target hit rate, and timeout rate.

Example:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_tp_sl_sweep.py --lane btc_eth_fade_or_follow --symbol BTC --side B
```

Focused single-setting report:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane btc_eth_fade_or_follow --symbol BTC --side B --exit-model r_multiple --stop-bps 15 --target-r 2.5 --diagnostics
```

## Current Read

BTC B-side, using 10/15/20/30 bps stops and 1.0R/1.5R/2.0R/2.5R targets:

- No tested combo was profitable after the 8 bps cost haircut.
- Best PF was only about 0.40.
- The 15 bps / 2.5R example had roughly 30% win rate, about 4% target hits,
  and negative average R.

ETH, same grid:

- No tested combo was profitable.
- Best PF was about 0.41.
- Failed-reclaim continuation was especially weak.

HYPE B-side alt range scalp, same grid:

- Still tiny sample: n=10.
- Best tested combo was 30 bps / 1.0R with PF about 1.44, positive median,
  and 60% win rate.
- This is watchlist-only, not enough for execution promotion.

## Decision

- Do not promote BTC/ETH to paper/live from the current simple fade/reclaim
  rules.
- Keep collecting clean 1m data and cascades.
- After each mature rebuild, Marvis should run the TP/SL sweep for BTC, ETH,
  and HYPE B-side and send Codex the top rows plus any material PF/median shift.
- Next Codex research step: add structure-aware stops such as event-VWAP
  invalidation and ATR-based stops, then compare them against the fixed-bps
  sweep.

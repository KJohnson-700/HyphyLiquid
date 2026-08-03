# Trailing Resolution Handoff - 2026-08-03

## Scope

Codex added an analysis-only BTC/ETH trailing-resolution sweep. This tests
Slim's reframing that BTC/ETH liquidation events may need longer holds and a
trailing stop instead of a short scalp TP/SL.

## Model

- Entry variants come from the existing BTC/ETH `fade_or_follow` backtest.
- Initial stop can be fixed bps, ATR, or event-VWAP invalidation.
- Trailing activates only after price moves `activation_r` times the initial
  stop distance in favor.
- After activation, the trailing stop follows by raw-price `trail_bps`.
- Same-candle assumptions remain conservative.
- No live or paper execution behavior changed.

## Commands

Full research sweep:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_trailing_sweep.py --symbol BTC --side B --horizons 30,60,120,240 --top 40
```

Focused candidate family:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_trailing_sweep.py --symbol BTC --side B --horizons 120,240 --stop-models event_vwap,fixed_bps --initial-stops-bps 30,50 --vwap-buffers-bps 15,25 --activation-rs 1,1.5,2 --trail-bps 10,15,25 --top 25
```

ETH check:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_trailing_sweep.py --symbol ETH --horizons 30,60,120 --top 40
```

Stability check for the current BTC watchlist candidate:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_trailing_stability_report.py
```

Use the stability report to compare `120m@120m` against `120m@240m`.
If the result only works in the stricter 240m-mature subset, treat it as
coverage bias until the broader 120m sample agrees.

## Current Read

BTC B-side:

- The broad 30/60/120m run improved the best result but did not prove an edge:
  best broad PF was about 0.99 on failed-reclaim continuation.
- A narrower 120/240m candidate sweep found positive pockets:
  failed-reclaim continuation, event-VWAP stop plus 15-25 bps buffer,
  activation around 2R, 10 bps trail, PF about 1.55-1.60.
- The positive rows are n=30 because 240m coverage excludes newer events.
  Treat them as a watchlist hypothesis, not promotion evidence.
- Stability report update:
  - `120m@120m`: n=37, PF 1.01, median -0.0863%.
  - `120m@240m`: n=30, PF 1.60, median +0.0750%.
  - `240m@240m`: n=30, PF 1.59, median +0.1081%.
  - Read: the broader 120m sample does not yet confirm the edge. The
    candidate is still a coverage-sensitive watchlist lane, not a paper-trade
    lane.

ETH:

- Longer trailing exits did not rescue ETH.
- Best checked rows stayed negative, with PF below 0.70.
- Stability spot check on the same failed-reclaim/event-VWAP/2R/10bps model:
  ETH A-side PF 0.50 at 120m and 0.47 at 240m; ETH B-side PF 0.31 at 120m
  and 0.27 at 240m. ETH remains rejected under this framing.

## Decision

- BTC/ETH scalp framing is still rejected.
- BTC B-side failed-reclaim continuation becomes the next watchlist lane.
- Promotion gate stays strict: require more mature windows, n>=100, positive
  median, PF trending above 1.5 after costs, and stable behavior outside the
  exact 240m-coverage subset.
- Marvis should run the focused candidate family after each mature rebuild and
  report whether the n=30 pocket survives as it grows.
- Marvis should also run `scripts/run_trailing_stability_report.py` after the
  focused sweep. Escalate only if the broad sample and mature subset both show
  positive median and PF above 1.5 after costs.

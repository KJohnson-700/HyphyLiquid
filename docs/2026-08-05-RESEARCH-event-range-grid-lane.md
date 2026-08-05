---
date: 2026-08-05
type: research
project: hyphyliquid
status: active
---

# Event Range Grid Lane

## Why

Some assets are not working cleanly under the current cascade fade/follow framing, but still show long ranging periods. A classic always-on grid is dangerous during liquidation continuation, so this lane tests a narrower idea:

`range regime + liquidation sweep into a Bollinger edge -> bounded grid basket -> exit back to mid-band`

This is research-only.

## Rules

- Symbols: `SOL`, `HYPE`, `DOGE`, `BNB`, `xyz:GOLD`, `xyz:SILVER`.
- No BTC/ETH execution changes.
- No live execution.
- No martingale.
- Entry only after a liquidation event that sweeps a Bollinger band edge.
- Default band buckets: `normal`, `wide`.
- Basket target: Bollinger mid-band.
- Stop: outer band plus buffer.
- Grid: fixed bps spacing, capped levels.
- Conservative simulation: stop wins over target on the same bar.

## Code

- `src/strategy/grid_backtest.py`
- `scripts/run_grid_backtest.py`
- `tests/test_grid_backtest.py`

## First Read

All research symbols:

- HYPE: n=29, PF 1.28, avg +0.0213%, median -0.0906%.
- HYPE wide bucket: n=7, PF 4.20, median +0.1535%.
- HYPE normal bucket: n=22, PF 0.68, median -0.0955%.
- SOL, DOGE, xyz:GOLD, xyz:SILVER are too small and/or negative.
- BNB produced no grid trades under default rules.

Focused HYPE B-side:

- HYPE side=B: n=20, PF 1.81, avg +0.0521%, median -0.0721%.
- HYPE side=B wide bucket: n=5, PF 5.03, avg +0.1721%, median +0.1535%.
- HYPE side=B normal bucket: n=15, PF 1.17, avg +0.0121%, median -0.1057%.

## Interpretation

This does not promote grid trading yet. It says HYPE wide-range liquidation grids are worth watching, but the sample is tiny and the full HYPE B-side median is still negative.

The next useful test is a parameter sweep on:

- grid spacing
- max levels
- stop buffer
- max hold
- allowed band buckets

Promotion bar should be stricter than the first read:

- n >= 50
- PF > 1.5
- median net return > 0
- top-win concentration acceptable
- no evidence that losses are caused by stop clustering during true continuation

## Focused HYPE B-Side Sweep

Codex added `scripts/run_grid_sweep.py` and ran a narrowed first pass on the highest-signal pocket:

`HYPE + side=B + band_width=wide`

Sweep space:

- spacing: 5, 10, 15, 25 bps
- max levels: 1, 2, 3
- stop buffer: 5, 10, 20 bps
- hold: 30, 60, 120 minutes
- min_n watch threshold for this tiny first pass: 5

Best row by PF:

- buckets: `wide`
- spacing: 10 bps
- max levels: 3
- stop buffer: 20 bps
- hold: 30 minutes
- n=7
- WR=42.9%
- avg +0.0770%
- median -0.0529%
- PF 2.01
- top_win_share 53.3%
- watch_pass=False

Decision: the focused grid sweep did **not** pass. PF improved, but median stayed negative and the best row was outlier-heavy. Keep HYPE grid as research/watch only until the sample grows and a sweep row clears median > 0 with acceptable top-win concentration.

---
project: HyphyLiquid
topic: tp-sl-settings
date: 2026-08-03
status: research
owner: Codex
---

# TP/SL Settings Research

## Bottom Line

HyphyLiquid should not evaluate BTC/ETH or alt lanes as live-ready until each
lane has explicit stop-loss and take-profit logic. The current BTC/ETH
`fade_or_follow` backtest uses a fixed 15-minute exit, so it is a signal
diagnostic, not a complete trading strategy.

The right initial target shape is R-based:

- `1R` = the stop distance.
- Test TP at `1.5R`, `2.0R`, and `2.5R`.
- Keep timeout exit as a safety rail.
- Apply costs/slippage before judging PF.

Slim's instinct is right: a strategy can win with a lower win rate if take
profit is materially larger than stop loss. But the units matter.

## Unit Clarification

Do not use raw `12-15%` price stops for 1m liquidation scalps.

If `12-15%` means raw price movement:

- BTC/ETH/HYPE will rarely reach a `30-35%` price target in a scalp window.
- The position size would become tiny to keep account risk under $5-$10.
- This does not match the liquidation-reclaim strategy.

If `12-15%` means leveraged position ROE at `10x`:

- That is roughly `1.2-1.5%` raw price movement.
- TP at `2.5R` is roughly `3.0-3.75%` raw price movement.
- This may still be too wide for a 5-15 minute fade setup, but can be tested.

For HyphyLiquid, define stops in raw price distance/bps, then calculate notional
from account risk.

Example:

- Account risk: `$10`
- Stop distance: `0.15%` raw price
- Notional size: `$10 / 0.0015 = $6,666`
- Margin at 10x: about `$667`
- TP at `2.5R`: `0.375%` raw price

This matches the $1,000 bankroll and 0.5-1.0% risk rule much better.

## Current Indicators And Settings

### BTC/ETH `fade_or_follow`

- Liquidation cascade cluster: `60s`.
- Event VWAP: weighted average price of clustered liquidation fills.
- Reclaim window: `3m`.
- Entry timing: first 1m candle after the cascade, max `2m` late.
- Current exit: fixed `15m` hold.
- Current stop loss: none in the backtest.
- Current take profit: none in the backtest.

Interpretation: this lane currently tells us whether the entry signal has any
directional value. It does not yet tell us whether the trade can be managed
profitably.

### Alt `range_sweep_liq_scalp`

- Bollinger Bands: `20` periods on `1m` candles.
- Standard deviation: `2.0`.
- Entry confirmation: wick outside the outer band, close back inside.
- Target: mid-band.
- Stop: outer band plus `5 bps` buffer.
- Timeout: `15m`.
- Cost haircut: `8 bps` round trip.

Current HYPE B-side structural read:

- Average target distance: about `0.2926%`.
- Average stop distance: about `0.1092%`.
- Average reward/risk: about `3.7R`.
- Normal-width bucket had the best structure, but sample is tiny.

## Research Notes

Hyperliquid TP/SL orders are triggered by mark price. TP/SL market orders have
a broad slippage tolerance, while limit TP/SL orders let the trader control
fill tolerance at the cost of possible non-fill.

Wick Hunter's public docs define stop-loss distance as raw price percent, not
ROE. That distinction matters because leverage changes margin/ROE, but it does
not change the price where the stop triggers.

Wick Hunter's liquidation-bot entry guidance centers on liquidation events
plus VWAP/VWMA band displacement. Its example uses 1m timeframe, 20-period VWMA,
and manually set band thresholds. That supports testing HyphyLiquid's current
1m, 20-period band logic, but it does not prove profitability.

Wick Hunter's TP guide supports single TP, multiple TP, and trailing logic.
For HyphyLiquid v1, start simple with one TP, one SL, and a timeout. Add partial
take-profits or trailing only after the single-target model is understood.

## Recommended Test Grid

### BTC/ETH

Test BTC and ETH separately, by side, never blended.

- Stop models:
  - `event_vwap_invalid`: stop beyond event VWAP by `5-15 bps`.
  - `atr_stop`: `0.5x`, `0.75x`, `1.0x` 1m ATR.
  - `fixed_bps`: `10`, `15`, `20`, `30` bps.
- TP models:
  - `1.0R`, `1.5R`, `2.0R`, `2.5R`.
  - optional VWAP/mid-band target as a separate model.
- Timeout:
  - `5m`, `15m`, `30m`.
- Required report:
  - win rate,
  - average R,
  - median R,
  - PF after costs,
  - max adverse excursion,
  - max favorable excursion,
  - top-win share of gross profit.

### HYPE / Alts

Keep the existing mid-band target model, but add R diagnostics:

- Current stop: outer band + `5 bps`.
- Current target: mid-band.
- Add optional stop buffers: `5`, `10`, `15` bps.
- Add optional minimum R filter:
  - skip trades where target/stop is below `1.5R`,
  - compare against `2.0R` and `2.5R`.
- Keep HYPE B-side separate.
- Do not use compressed-band cap as preferred logic until data reverses.

## Decision

The next code step should be a TP/SL simulator for BTC/ETH that converts every
candidate trade into R-multiple outcomes. Until then, the BTC/ETH lane is not
strategy-complete.

Sources:

- Hyperliquid TP/SL docs: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl
- Hyperliquid liquidation docs: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Wick Hunter stop-loss settings: https://docs.wickhunter.io/en/articles/11765383-stop-loss-settings
- Wick Hunter entry settings: https://docs.wickhunter.io/en/articles/11826992-entry-settings
- Wick Hunter take-profit guide: https://guide.wickhunter.io/trading-bots/create-a-trading-bot/take-profit-tp

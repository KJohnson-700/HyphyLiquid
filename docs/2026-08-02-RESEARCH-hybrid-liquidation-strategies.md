---
title: Hybrid Liquidation Strategy Sweep
date: 2026-08-02
status: research-note
scope: HyphyLiquid BTC/ETH/SOL/HYPE on Hyperliquid
---

# Hybrid Liquidation Strategy Sweep

## Bottom Line

The strongest fit for HyphyLiquid is still a liquidation-cascade bot, but the entry should be gated by one or two independent confirmations. The most promising hybrids are:

1. Liquidation burst + VWAP/VWMA displacement.
2. Liquidation burst + OI/funding squeeze context.
3. Liquidation burst + order-book absorption/reclaim.
4. Liquidation heatmap magnet + burst confirmation.

Do not add RSI/MACD/Bollinger as primary logic yet. Do not add ML, DCA, martingale, cross-venue execution, or more assets.

## Source Notes

- Hyperliquid official WebSocket docs expose the required public streams: `trades`, `l2Book`, `bbo`, `candle`, `activeAssetCtx`, and related private streams such as `orderUpdates`, `userFills`, and `userFundings`.
- Hyperliquid liquidation docs explain that liquidations are attempted through market orders sent into the book, which supports using public trade bursts and book response as a first-class signal surface.
- Wick Hunter's public docs/repo repeatedly point to liquidation data plus VWAP/VWMA displacement as the core countertrade pattern.
- HyperPerps/HyprPulse/MMFlow style tools point to a second family of ideas: liquidation clusters, OI skew, funding, stop pools, and whale/retail positioning as context rather than execution dependencies.

## Recommended Hybrids

### 1. Cascade Fade With VWAP/VWMA Displacement

Trigger:
- Detected liquidation burst or single large forced-flow proxy.

Confirm:
- Price is stretched away from VWAP/VWMA by symbol-specific threshold.
- Fade only when liquidation direction agrees with the stretch:
  - Long liquidations below lower band -> long mean-reversion candidate.
  - Short liquidations above upper band -> short mean-reversion candidate.

Why it fits:
- This is the closest public analogue to Wick Hunter.
- Uses data HyphyLiquid already captures: trades and candles.
- Easy to backtest after deduping events.

Initial test grid:
- BTC/ETH: 1m or 5m VWMA, 20-60 period, 0.5%-2.0% displacement.
- SOL/HYPE: wider displacement and larger volume filters because noise is higher.

Risk:
- A liquidation can be continuation, not exhaustion. Requires stop and no averaging.

### 2. Cascade Fade With OI/Funding Squeeze Context

Trigger:
- Liquidation burst.

Confirm:
- OI has recently risen into the move, then drops or stalls during the burst.
- Funding/predicted funding shows crowded side, but do not use funding alone.

Why it fits:
- Hyperliquid `activeAssetCtx` exposes mark, funding, predicted funding context, and open interest.
- This converts funding from failed primary trigger into a regime/context filter.

Example logic:
- Long fade after downside liquidation only if OI was elevated or rising before the drop and funding was positive/crowded-long.
- Short fade after upside liquidation only if OI was elevated or rising before the pump and funding was negative/crowded-short.

Risk:
- Funding on mainnet is often too smooth. Treat it as a weak filter.

### 3. Burst Plus Order-Book Absorption/Reclaim

Trigger:
- Liquidation burst.

Confirm:
- During/after the burst, price fails to keep moving through the book.
- BBO spread normalizes.
- Mid reclaims the burst VWAP or prior 1m open.
- Optional: book imbalance flips against the liquidation direction.

Why it fits:
- Hyperliquid official streams include `l2Book`, `bbo`, and trades.
- This avoids catching falling knives by requiring evidence the forced flow is absorbed.

Testable entry:
- Wait 15-90 seconds after event.
- Enter only if price reclaims event VWAP by X bps and spread is below max threshold.

Risk:
- Waiting for reclaim reduces profit but should improve false-positive filtering.

### 4. Liquidation Magnet Then Exhaustion Fade

Trigger:
- Price moves toward a nearby liquidation cluster from HyperPerps-like heatmap data.

Confirm:
- Actual Hyperliquid trade burst occurs near the cluster.
- Then reclaim/absorption condition fires.

Why it fits:
- HyperPerps has BTC/ETH/SOL heatmap data, but HYPE returns no useful sample.
- Use heatmap data as context only, not as execution dependency.

Asset routing:
- BTC/ETH/SOL: heatmap context allowed.
- HYPE: direct Hyperliquid-only logic, no heatmap gate.

Risk:
- Third-party heatmap data should not be a hard dependency for live execution.

## Reject For Now

- Pure funding mean reversion: already failed as a primary signal on mainnet.
- Pure liquidation count: duplicate burst detections inflate confidence.
- Multi-asset correlation trades: not the same strategy class.
- DCA/grid/hedge recovery logic: violates risk posture for a $1,000 bankroll.
- ML classifier: out of scope before enough own live/paper trades.

## Next Implementation Order

1. Dedupe/cluster liquidation detections into one event per symbol/side/time window.
2. Add VWAP/VWMA displacement columns to the liquidation backtest.
3. Add OI/funding context as optional columns, not as a trading rule yet.
4. Compare baseline cascade fade vs VWMA-gated fade vs reclaim-gated fade on 1h/4h horizons.
5. Split results by BTC/ETH/SOL/HYPE and do not combine HYPE with heatmap-gated results.

## Sources

- Hyperliquid WebSocket docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
- Hyperliquid subscriptions docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- Hyperliquid liquidation docs: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Hyperliquid perpetual info endpoint docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Wick Hunter repo: https://github.com/WickHunter/Wick-Hunter
- Wick Hunter entry settings: https://docs.wickhunter.io/en/articles/11826992-entry-settings
- Wick Hunter liquidation bot overview: https://docs.wickhunter.io/en/articles/11827010-what-is-the-original-liquidation-bot
- HyperPerps overview: https://hyperperps.app/
- HyprPulse overview: https://hyprpulse.com/
- MMFlow OI/liquidation analytics: https://www.mmflow.ai/oi

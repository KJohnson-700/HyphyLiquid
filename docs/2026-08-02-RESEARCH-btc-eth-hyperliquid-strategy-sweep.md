---
title: BTC/ETH Hyperliquid Strategy Sweep
date: 2026-08-02
status: research-note
scope: BTC/ETH only
---

# BTC/ETH Hyperliquid Strategy Sweep

## Bottom Line

BTC and ETH should not be treated like SOL/HYPE chop scalps. They are deeper, more directional, and more macro-sensitive. The best HyphyLiquid fit is a liquidation-aware derivatives-flow strategy with regime routing:

1. Trend day: trade with the liquidation cascade after failed reclaim.
2. Exhaustion day: fade the cascade after absorption/reclaim.
3. Range day: use VWAP/band extremes, but only with liquidation or order-flow confirmation.

The bot should not pre-commit to always fading liquidations. BTC/ETH need a `fade_or_follow` decision layer.

## Why BTC/ETH Need Different Logic

- Hyperliquid liquidations are sent into the order book as market orders when possible, so public trades and book response are directly relevant.
- BTC and ETH have deeper books than alts, so one large trade is less informative by itself. The response after the burst matters more than the burst alone.
- Funding on Hyperliquid settles hourly and is useful as a crowding/regime input, but prior mainnet tests showed it is too smooth to be a primary signal.
- BTC/ETH funding impact notional is larger than other assets, so their funding behavior should be interpreted separately from SOL/HYPE.

## Strategy Candidates

### 1. Liquidation Exhaustion Reclaim

Use when the market flushes into liquidation flow but fails to continue.

Trigger:
- Large same-side trade burst or liquidation proxy.
- Optional heatmap/liquidation pocket nearby.

Confirm:
- Price reclaims event VWAP or pre-burst 1m/5m level.
- Spread normalizes after the burst.
- L2/BBO no longer shows one-sided pressure.
- OI stalls or falls after the forced flow.

Entry:
- Enter after reclaim, not during the flush.
- Use IOC/limit entry with bounded slippage.

Exit:
- TP1 at VWAP or pre-burst midpoint.
- TP2 at opposite liquidity pocket only if flow continues.
- Hard stop beyond burst extreme.

Best fit:
- BTC and ETH after sharp liquidation flushes into known support/resistance.

### 2. Liquidation Continuation / Failed Reclaim

Use when the market absorbs no bounce and forced flow continues.

Trigger:
- Liquidation burst.
- Price does not reclaim event VWAP within 1-5 minutes.

Confirm:
- BBO/book remains one-sided.
- OI is falling while price continues in liquidation direction.
- Funding/crowding was aligned with the liquidated side before the move.

Entry:
- Follow the cascade on a failed reclaim or retest.
- Avoid chasing the initial candle.

Exit:
- Take profit into next liquidation/heatmap pocket.
- Tight time stop: if no continuation quickly, flatten.

Best fit:
- BTC/ETH trend or news-driven sessions.

### 3. VWAP Regime Router

This is the bridge between the user's scalping experience and BTC/ETH derivatives flow.

Regime:
- Above VWAP and holding: only long continuation or short-liquidation squeeze setups.
- Below VWAP and holding: only short continuation or long-liquidation flush setups.
- Chopping around VWAP: only fade extremes with liquidation confirmation.

Entry filters:
- Event VWAP reclaim for fades.
- VWAP rejection for continuation.
- Minimum distance from VWAP before considering a fade.

Why it fits:
- VWAP gives a simple intraday fair-value anchor without turning the bot into an indicator soup.

### 4. OI/Funding Crowding Filter

Funding and OI should classify setup quality, not trigger trades.

Useful reads:
- Rising OI + price up + positive funding: crowded longs; downside liquidation risk increases.
- Rising OI + price down + negative funding: crowded shorts; upside squeeze risk increases.
- Falling OI during move: deleveraging; do not blindly fade until price stabilizes.
- Funding alone: weak/no trade.

Best use:
- Decide whether a liquidation burst is likely exhaustion or continuation.

### 5. Heatmap Magnet, But Confirmation Required

Use HyperPerps/LiqFlow/Fuelmaps-style levels as context.

Rules:
- Nearby pocket can define target or danger zone.
- Never enter solely because a level exists.
- Require Hyperliquid-direct trade/book confirmation.

Best fit:
- BTC/ETH, because heatmap models are more liquid and better-covered than HYPE.

## Indicators Worth Testing

Primary:
- Event VWAP.
- Session VWAP or rolling VWAP/VWMA.
- OI delta.
- Funding/predicted funding.
- BBO spread.
- L2 top-of-book imbalance.
- Realized volatility / ATR for stop width.

Secondary:
- Bollinger/Keltner bandwidth as regime filter.
- 7d high/low proximity as macro context.
- Liquidation pocket distance.

Avoid for now:
- RSI/MACD as primary triggers.
- Pure grid.
- Martingale/DCA.
- Pure funding arb.
- Options-style structures such as iron condor; Hyperliquid perps do not have expiries/options legs.

## WebSocket / Fetching Recommendations

Already useful:
- `trades`: forced-flow proxy and liquidation burst detection.
- `l2Book`: absorption, sweep depth, book imbalance.
- `bbo`: spread normalization and execution safety.
- `candle`: 1m/5m/VWAP features.
- `activeAssetCtx`: mark, funding, open interest, oracle.

Add before live execution:
- `orderUpdates`: know whether parent/TP/SL orders are live.
- `userFills`: execution truth and slippage.
- `userFundings`: hourly funding PnL accounting.
- `clearinghouseState` / `webData3`: position reconciliation.

Sorting and storage:
- Treat trade identity as `(block_time, coin, tid)`, not `tid` alone.
- Cluster liquidation detections by symbol, side, and short time window before backtesting.
- Store event-level features at detection time: event VWAP, pre/post price, OI before/after, funding, spread, top-book imbalance.

## Execution Tips

- Use `normalTpsl` grouping for parent entry with attached TP/SL.
- Use IOC/limit for entries when chasing would create bad fills.
- Use reduce-only on all exits.
- Do not leave an accepted entry naked if TP/SL child order fails.
- Add a stale signal timeout. BTC/ETH signals decay quickly after the burst.
- Split strategy result by asset; do not combine BTC and ETH metrics until each is independently sane.

## Proposed BTC/ETH Test Matrix

Baseline:
- Deduped liquidation fade, 1h and 4h horizons.

Scalp variants:
- Fade after event VWAP reclaim, max hold 5/15/30 minutes.
- Continue after failed reclaim, max hold 5/15/30 minutes.

Filters:
- VWAP distance threshold.
- OI delta before/after event.
- Funding crowding direction.
- Spread max threshold.
- Top-book imbalance flip.

Decision:
- If fade and continuation both work in different regimes, implement a `fade_or_follow` classifier using only rule-based features.

## Current Recommendation

For BTC/ETH, build `liquidation_fade_or_follow`:

1. Detect and dedupe liquidation burst.
2. Measure event VWAP and immediate book response.
3. Wait up to 1-5 minutes.
4. If reclaim/absorption: fade.
5. If failed reclaim plus continued pressure: follow.
6. If neither: no trade.

This preserves the liquidation thesis while respecting that BTC/ETH cascades can be either exhaustion or continuation.

## Sources

- Hyperliquid liquidations: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Hyperliquid contract specs: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
- Hyperliquid websocket subscriptions: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- Hyperliquid order types: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types
- Hyperliquid exchange endpoint: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/exchange-endpoint
- Hyperliquid Python SDK example: https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/examples/basic_adding.py
- Wick Hunter repo: https://github.com/WickHunter/Wick-Hunter
- Fuelmaps BTC liquidation heatmap explainer: https://fuelmaps.io/learn/btc-liquidation-heatmap/
- BackQuant liquidations explainer: https://www.backquant.com/learn/liquidations
- LiqFlow ETH Hyperliquid liquidation page: https://liqflow.app/coin/ETH
- Stingray Hyperliquid algo strategy guide: https://stingray.fi/blog/hyperliquid-algo-trading-strategies-2026/

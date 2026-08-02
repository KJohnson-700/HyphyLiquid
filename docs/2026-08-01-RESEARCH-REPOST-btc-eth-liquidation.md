# HyphyLiquid Research Repost — BTC/ETH Hyperliquid Liquidation Bot

**Date:** 2026-08-01  
**Purpose:** Preserve the public research sweep for AI editors and constrain future decisions.

## Canonical decision
Build one automated BTC/ETH liquidation-cascade counter-trade bot on Hyperliquid. Public research supports the engineering direction but provides no reliable proof of profitability. The edge must be demonstrated with our own event logs, fills, costs, and out-of-sample results.

## What the public market shows

GitHub search results are dominated by monitoring and infrastructure:

- BotsOnBlock/HyperLiquidation-Bot: margin-ratio Telegram alerting.
- Daezo55/hyperliquid-liquidation: liquidation Telegram monitoring.
- syzz-xd/hyperliquid-liquidations: real-time liquidation alerts to Discord.
- cobecheng/hypertracker-bot: wallet tracking and liquidation monitoring.
- CShear/ref-perp-bot: reference perpetual bot with whale/funding/liquidation components.
- KonScanner/hyperliquid-trader-tracker: real-time wallet and position tracking.
- titouannwtt/freqtrade-ultimate: Hyperliquid-enabled trading/backtest framework.

Primary search: https://github.com/search?q=hyperliquid+liquidation+bot&type=repositories

The implication is clear: alerts are common; a robust, measured counter-trade engine is not. Do not confuse an event feed with a tradable signal.

## Data and strategy requirements

The detector must preserve raw events and normalize at least:

- market, side, size/notional, liquidation price, event timestamp;
- ingestion timestamp and processing latency;
- mid/bid/ask, spread, depth proxy, volatility;
- open interest, funding, recent return, and cascade grouping;
- signal decision, order acknowledgement, fill price, fees, funding, slippage, and exit reason.

A long liquidation is forced selling and may create a long counter-trade opportunity only after continuation risk and liquidity are evaluated. A short liquidation is forced buying and may create a short opportunity under the same conditions. No automatic market order should be triggered by liquidation alerts alone.

## Social-source limitations

X search returned a JavaScript/browser error. Reddit returned anti-bot verification. YouTube returned no rendered results. Therefore this repost does not cite unverifiable social claims. Any later manual sources must include original URL, author/channel, date, mechanism, raw evidence, and costs. Screenshots and claimed PnL are leads, not validation.

NotebookLM was not readable because the supplied notebook URL redirected to Google sign-in. Merge its export later; do not silently represent it as reviewed.

## Scope guardrails for AI editors

Keep:

- BTC and ETH only;
- Hyperliquid only;
- official Python SDK;
- no ML before 100+ own live trades;
- no second strategy from day one;
- leverage cap 10x;
- max risk 1% bankroll per trade;
- max three open positions, correlated assets treated as one;
- daily 3% and weekly 5% circuit breakers;
- halt after three consecutive losses;
- stop below $600 bankroll;
- reduce-only closing orders;
- testnet proof, paper trading, $50 canary, then staged ramp.

Reject PAXG standalone trading, gold/silver scope, other venues, MT5/MQL5 ports, CCXT, Web3.py, TensorFlow/PyTorch, and social-copy strategies.

## Next implementation implications

1. Build or harden a raw liquidation-event capture layer.
2. Add configurable BTC/ETH minimum event-notional and liquidity filters.
3. Group events into one cascade and enforce a cooldown.
4. Backtest fees, funding, slippage, latency, missed events, and partial fills.
5. Produce daily detected/eligible/rejected/executed/counterfactual metrics.
6. Validate out of sample before changing thresholds.

## Confidence

High: infrastructure dominates the public repository landscape; the existing scope and risk framework remain appropriate.  
Medium: wallet-flow and event-stream context may improve filtering.  
Low: social PnL, win-rate, and latency claims without raw logs.

## Source list

- https://github.com/BotsOnBlock/HyperLiquidation-Bot
- https://github.com/Daezo55/hyperliquid-liquidation
- https://github.com/syzz-xd/hyperliquid-liquidations
- https://github.com/cobecheng/hypertracker-bot
- https://github.com/CShear/ref-perp-bot
- https://github.com/KonScanner/hyperliquid-trader-tracker
- https://github.com/titouannwtt/freqtrade-ultimate
- https://github.com/search?q=hyperliquid+liquidation+bot&type=repositories
- https://www.reddit.com/search/?q=hyperliquid%20liquidation%20bot
- https://www.youtube.com/results?search_query=hyperliquid+liquidation+bot
- https://x.com/search?q=hyperliquid%20liquidation%20bot&src=typed_query

---
title: NotebookLM Q&A Expansion - HyphyLiquid Research
project: hyphyliquid
asset: crypto
strategy: btc-eth-cascade
venue: hyperliquid
status: decision-support
date: 2026-08-02
notebooklm: https://notebook.google.com/notebook/ca490abb-5931-46bb-80f3-f3df935a20c5
notebook_id: ca490abb-5931-46bb-80f3-f3df935a20c5
---

# NotebookLM Q&A Expansion - HyphyLiquid Research

## Purpose

This note captures the expansion prompts run against the `HyphyLiquid Research` NotebookLM notebook after the source sweep was loaded. It is intended for AI editors working in the HyphyLiquid repo so they inherit the reasoning, not only the source list.

## Prompts Hit

1. Why keep v1 scoped to BTC/ETH liquidation cascade trading on Hyperliquid only?
2. Which Hyperliquid endpoints/subscriptions should be implemented next, separated into public market data vs authenticated user data?
3. What risk controls, kill switches, and execution safeguards must exist before live trades?
4. What data, metrics, and invalidation rules are needed for backtesting?
5. What should HyphyLiquid explicitly not build in v1?
6. What source gaps, uncertain assumptions, or Slim decisions remain?

## Expanded Decisions

### Keep BTC/ETH + Hyperliquid Only

NotebookLM reinforced the existing scope. BTC/ETH are the right v1 assets because they have the deepest liquidity and distinct volume profiles. WickHunter-style liquidation logic explicitly treats BTC/ETH differently from altcoins. Hyperliquid is the right venue because liquidations, margin state, funding, mark price, and public trade flow are unusually observable compared with centralized exchanges.

Decision: no SOL/HYPE expansion in v1.

### Funding Cadence Correction

NotebookLM repeatedly surfaced the same conflict: project docs mention 8h funding, while current Hyperliquid sources describe fixed 1-hour funding. Implementation should support hourly funding capture and hourly funding P&L, with daily rollups for journal/risk reporting.

Decision needed from Slim: approve updating AGENTS.md and vault docs from "8h" to hourly.

### Public Data To Build Next

Implement these first for research/data capture:

- `trades` for public trade flow and possible liquidation-flow inference.
- `l2Book` for order book depth and slippage modeling.
- `candle` for VWAP/VWMA calculations.
- `allMids` for fast venue-wide mid prices.
- `activeAssetCtx` or `allDexsAssetCtxs` for funding, open interest, mark price, oracle price, premium, and 24h volume.
- `fundingHistory` for historical funding extremes.
- `meta` / `metaAndAssetCtxs` for universe metadata, leverage limits, size decimals, and current asset context.

Third-party heatmap APIs such as HyperPerps, LiqFlow, and CoinPerps may be used only as research/validation inputs. They should not be execution dependencies.

### Authenticated Data To Build After Auth Proof

Implement these only for the bot's own wallet/account:

- `clearinghouseState` for account value, margin, unrealized P&L, and risk gating.
- `userEvents` for own liquidation, funding, fills, and non-user cancel events.
- `orderUpdates` for order lifecycle monitoring.
- `userFills` for execution/P&L reconciliation.
- `userFundings` for funding payments on the hour.
- `openOrders` for reconciliation and stuck order checks.

### Execution Safeguards

Hyperliquid-specific safeguards to enforce:

- Use mark/oracle-aware logic; do not make liquidation or stop decisions from last trade price alone.
- Normalize outgoing prices to Hyperliquid precision rules, including the 5 significant-figure constraint.
- Use slippage buffers for market and stop-market behavior.
- Subscribe to quote/book data before submitting market orders.
- Use reduce-only for closing orders.
- Use environment variables for keys; no secrets in source.
- Complete testnet auth proof before live mainnet.

### Risk Controls

NotebookLM surfaced useful general controls but some suggested numbers differ from AGENTS.md. Project law wins:

- Max risk per trade: 1% of bankroll.
- Max leverage: 10x.
- Max open positions: 3.
- Daily loss breaker: 3%.
- Weekly loss breaker: 5%.
- 3 consecutive losses: halt 24h.
- Bankroll below $600: stop and re-evaluate with Slim.
- Every trade must pass `src/risk.py` before execution.

Useful addition for later discussion: isolated margin may reduce account-level blast radius, but this should be confirmed against the project execution model before changing hard rules.

### Backtest Data To Capture

Before live trading, capture:

- BTC/ETH candles at 1m and 5m.
- Funding history at hourly resolution.
- Mark price, oracle/index price, mids, and premium.
- Open interest / asset context snapshots.
- L2 book snapshots or sampled depth around candidate events.
- Public trade stream around liquidation-like bursts.
- Any third-party heatmap observations as non-execution research labels.

Backtests must include fees, estimated slippage, funding P&L, missed fills, stop logic, and realistic order constraints.

### Honest Metrics

Report:

- Total return and monthly return.
- Win rate.
- Profit factor.
- Max drawdown.
- Average win/loss.
- Mean holding time.
- Trades per month.
- Funding P&L.
- Fees and slippage.
- Live/paper vs backtest divergence.

Do not promote results that only work before fees/slippage/funding.

### Invalidation Conditions

Pause or redesign if:

- Paper/live results materially diverge from backtests.
- Slippage consumes the expected bounce edge.
- Strategy fails during strong trend regimes where cascades continue rather than revert.
- Reconciliation errors leave unknown/stuck positions.
- Drawdown/risk breakers trigger early in paper.
- The apparent edge depends on bugs, unmodeled fills, or lookahead artifacts.

### Explicit V1 Rejections

Do not build:

- DCA/martingale.
- Copy-trading whale wallets.
- SOL/HYPE or broad multi-asset scanning.
- ML or autonomous AI trade selection.
- Dashboard-first workflow.
- Strategy thresholds derived from X/Twitter or influencer videos.

Social sources are context only. They can suggest alert types and examples, not trading rules.

## Open Slim Decisions

1. Confirm the funding cadence documentation update from 8h to hourly.
2. Decide whether third-party heatmap APIs are allowed as research/backtest inputs.
3. Decide whether v1 should prioritize actual liquidation detection first or keep funding extremes as the first shippable proxy.
4. Confirm whether isolated margin should become a hard v1 execution rule.
5. Confirm the wallet/testnet path, because Hyperliquid testnet faucet access may require prior mainnet deposit with the same wallet.

## Bottom Line

The expanded NotebookLM prompts support continuing Week 1 implementation, but the next work should remain narrow: data capture, WebSocket subscriptions, auth proof, and a backtest harness around funding extremes, VWAP/VWMA stretch, OI skew, and liquidation-context labels. No new strategy class.

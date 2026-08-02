---
title: Hyperliquid BTC/ETH Liquidation Bot Research Sweep
project: hyphyliquid
asset: crypto
strategy: btc-eth-cascade
venue: hyperliquid
status: research-log
run_date: 2026-08-02 UTC / 2026-08-01 PT
notebooklm: https://notebook.google.com/notebook/ca490abb-5931-46bb-80f3-f3df935a20c5
---

# Hyperliquid BTC/ETH Liquidation Bot Research Sweep

## Scope

Research target: public material from GitHub, X/Twitter, Reddit, YouTube-searchable web results, and Hyperliquid docs that affects the HyphyLiquid BTC/ETH liquidation cascade counter-trade bot.

NotebookLM note: the provided NotebookLM URL appears private from this environment, so this report is written as a source pack and decision memo that can be imported into NotebookLM.

## Executive Takeaway

The project direction remains valid: BTC/ETH on Hyperliquid is the right venue/asset pairing for liquidation-cascade research because Hyperliquid exposes unusually transparent position, margin, funding, and liquidation-adjacent data. The best external pattern is not a broad AI trading bot or martingale DCA bot. It is a small, tightly risk-gated strategy that combines liquidation clusters, funding extremes, OI/positioning skew, VWAP/VWMA distance, and fresh volume confirmation.

HyphyLiquid should continue as a conservative BTC/ETH-only cascade bot with the risk module as the hard gate. Do not expand to SOL/HYPE, copy-trading, ML, broad AI agents, or DCA/martingale sizing in v1.

## High-Confidence Findings

### 1. Hyperliquid liquidations are transparent enough to support this edge

Official Hyperliquid docs describe liquidations as account-equity falling below maintenance margin. Positions are first sent as market orders to the book; if liquidation through the book fails deeply enough, the liquidator vault/backstop can take over. Hyperliquid uses mark price, not a single last-trade print, for liquidation mechanics.

Decision:
- Build around mark/oracle-aware risk, not last-price-only triggers.
- Use public market data first, then add user/account-event monitoring only for our own wallet.
- Avoid pretending a visible liquidation cluster guarantees direction. It is a probability map, not a signal by itself.

Source:
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations

### 2. Funding cadence appears to be hourly, not every 8 hours

Hyperliquid contract specs currently describe funding payments every hour. The existing AGENTS.md risk note says funding P&L is tracked daily "every 8h on HL." That may be stale or copied from CEX assumptions.

Decision:
- Ask Slim before editing hard-rule docs, but assume implementation should support hourly funding history/payments.
- `src/exchange/hyperliquid.py` already has `get_funding_history()`, which is directionally right.
- Future journal/risk work should track funding at hourly granularity and roll up daily.

Source:
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications

### 3. WickHunter is useful as a pattern library, not as a strategy to copy

WickHunter's public repo and docs describe counter-trading liquidation-driven wicks with VWAP/VWMA confirmation, min liquidation size filters, max open positions, volume filters, and explicit stop controls. The old GitHub README recommends separate handling for BTC/ETH because their volume profile differs from altcoins.

Decision:
- Keep the useful pieces: min liquidation notional, VWAP/VWMA distance, volume filter, max open positions, and account-level kill switch.
- Reject DCA/martingale logic for HyphyLiquid v1. That conflicts with $1,000 bankroll and hard risk rules.
- BTC/ETH-only scope is reinforced, not weakened.

Sources:
- https://github.com/WickHunter/Wick-Hunter
- https://docs.wickhunter.io/en/articles/11826992-entry-settings

### 4. Public heatmap APIs are useful as secondary research inputs

HyperPerps and similar tools claim to aggregate real Hyperliquid liquidation clusters from public positions and expose BTC/ETH/SOL heatmap data. One public endpoint is documented as `https://trade.hyperperps.app/api/public/heatmap/BTC`.

Decision:
- Treat third-party heatmap APIs as optional research/validation data, not as a trusted execution dependency.
- If added, wrap behind an adapter and compare against direct Hyperliquid-derived data.
- Do not block Week 1 auth/data-layer work on heatmap integration.

Sources:
- https://hyperperps.app/hyperliquid-liquidation-heatmap
- https://www.coinperps.com/hype-liquidation-heatmap
- https://liqflow.app/coin/ETH

### 5. X/Twitter is valuable for market examples, but too noisy for rules

Recent X posts show strong demand for Hyperliquid alerts around funding, price jumps, liquidation thresholds, whale wallets, and large BTC/ETH exposures. Whale stories also show repeated liquidation/re-entry behavior and high-leverage account blowups.

Decision:
- Use X as qualitative recon and example generation.
- Do not source strategy thresholds from X posts.
- Useful alert concepts: custom liquidation threshold alerts, whale wallet clusters, funding alerts, and large TWAP alerts.

Sources:
- https://x.com/ericonomic/status/2028393292006854715
- https://x.com/lookonchain/status/2017619069164568579
- https://x.com/HyperliquidNews/status/2017672001435893836
- https://x.com/aigmx_agent/status/2036662536683294779

### 6. Reddit reinforces conservative process, not new features

Reddit/algotrading material is less Hyperliquid-specific, but the useful consensus is familiar: paper trade, test out-of-sample, monitor live-vs-theoretical behavior, keep strategy rules quantifiable, and avoid premature ML.

Decision:
- Reinforces AGENTS.md ban on ML before 100+ own live trades.
- Keep v1 rule-based and auditable.
- Treat paper canary and live canary as required, not optional.

Sources:
- https://www.reddit.com/r/algotrading/
- https://fr.reddit.com/r/algotrading/comments/1e40bak/to_people_currently_running_a_live_strategy_whats/

### 7. YouTube-specific durable sources were weak from search alone

Search surfaced many landing pages, API guides, and heatmap pages, but not enough stable YouTube video metadata/transcripts to cite directly. Some pages embed video players, but this sweep should not treat non-transcript video content as evidence.

Decision:
- Use NotebookLM to ingest any chosen YouTube transcripts manually.
- Prefer official docs and code repos over influencer videos for implementation decisions.
- If YouTube is used later, require transcript capture and log the video URL, channel, date, and claims.

## Implementation Implications

### Keep

- BTC/ETH only for v1.
- Hyperliquid official Python SDK as primary integration.
- `src/risk.py` as mandatory gate before every order.
- Funding-extreme signal as the first simple cascade proxy.
- Read-only market data wrapper before authenticated execution.
- Testnet auth proof before live mainnet.

### Add Next

- WebSocket subscriptions for `trades`, `l2Book`, `candle`, and our own wallet `userEvents`.
- Funding history/predicted funding capture at hourly resolution.
- VWAP/VWMA distance confirmation.
- OI/positioning skew capture from `metaAndAssetCtxs`.
- Optional third-party heatmap adapter for research only.
- Data snapshots saved locally for backtests before any live canary.

### Reject For V1

- DCA/martingale position averaging.
- Copy-trading whale wallets.
- Broad multi-asset scanning.
- AI-agent autonomous trade selection.
- ML models trained on borrowed/synthetic data.
- SOL/HYPE expansion.
- Dashboard-first work.

### Decide With Slim

- Whether to update AGENTS.md and vault docs from "funding every 8h" to "funding hourly."
- Whether third-party heatmap APIs can be used as a non-execution research source.
- Whether v1 should detect actual external liquidations first, or keep funding-extreme as the first shippable proxy.

## Proposed Signal Stack

Minimum viable v1 signal:
1. Funding extreme on BTC or ETH.
2. Price stretched from VWAP/VWMA.
3. Volume/liquidity confirms the move is not a dead market.
4. OI/positioning skew agrees with one-sided leverage.
5. Risk module approves size, leverage, stop distance, daily/weekly state, and open-position count.

No trade if any hard risk gate fails.

## Source Inventory For NotebookLM

Primary sources:
- Hyperliquid liquidations: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Hyperliquid margining: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining
- Hyperliquid contract specs: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
- Hyperliquid API overview: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- Hyperliquid WebSocket subscriptions: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- Hyperliquid Python SDK: https://github.com/hyperliquid-dex/hyperliquid-python-sdk
- WickHunter repo: https://github.com/WickHunter/Wick-Hunter
- WickHunter entry settings: https://docs.wickhunter.io/en/articles/11826992-entry-settings

Secondary sources:
- HyperPerps heatmap/API: https://hyperperps.app/hyperliquid-liquidation-heatmap
- LiqFlow ETH heatmap: https://liqflow.app/coin/ETH
- CoinPerps Hyperliquid heatmap explainer: https://www.coinperps.com/hype-liquidation-heatmap
- Nautilus Trader Hyperliquid integration notes: https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/hyperliquid.md
- Passivbot Hyperliquid support: https://github.com/enarjord/passivbot

Social/recon sources:
- Ericonomic Hyperliquid alert bot thread: https://x.com/ericonomic/status/2028393292006854715
- Lookonchain ETH liquidation-risk example: https://x.com/lookonchain/status/2017619069164568579
- Hyperliquid News liquidation example: https://x.com/HyperliquidNews/status/2017672001435893836
- AIGMX whale-risk example: https://x.com/aigmx_agent/status/2036662536683294779
- Reddit algotrading process discussion: https://fr.reddit.com/r/algotrading/comments/1e40bak/to_people_currently_running_a_live_strategy_whats/

## Bottom Line

Mavis can continue. The research supports the current HyphyLiquid scope, with one likely correction: funding cadence should be treated as hourly unless Slim confirms otherwise. The next best build step is not more strategy complexity. It is data capture plus testnet auth, then a narrow backtest harness around funding extremes, VWAP/VWMA stretch, OI skew, and liquidation cluster context.

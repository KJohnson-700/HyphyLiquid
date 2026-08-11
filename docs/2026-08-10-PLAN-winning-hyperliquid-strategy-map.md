---
date: 2026-08-10
type: research-plan
project: hyphyliquid
status: active
---

# Winning Hyperliquid Strategy Map

## Thesis

The question is no longer whether profitable Hyperliquid bots exist. They do.

Public material shows active Hyperliquid bot families around liquidation flow, grid/range trading, funding and open-interest regimes, market making, and AI-assisted parameter selection. The project risk is not "there is no edge on Hyperliquid." The project risk is choosing a public strategy family, translating it into HyphyLiquid's exact bankroll/risk/execution constraints, and proving that the live-like paper path does not drift from real mainnet behavior.

## What External Evidence Supports

### 1. Liquidation Flow Is Tradable Raw Material

Hyperliquid liquidations are not hidden inside an opaque venue queue. The official liquidation docs say many liquidations are sent as market orders to the book, which means liquidation events can create real order-flow shocks. That supports HyphyLiquid's core premise: liquidation cascades can create post-event fade, follow, or exhaustion trades.

Use in HyphyLiquid:

- Keep liquidation events as the primary trigger.
- Stop treating every liquidation the same.
- Route by side, funding, OI, price reclaim/fail, and regime.

### 2. Funding and OI Belong in the Core Strategy

Hyperliquid funding is hourly and based on premium/oracle mechanics. That makes funding more than background carry; it is a live crowding and perp-basis signal. Current internal data already agrees: ETH side=A follow 60m under positive/elevated funding is the current v1 front-runner.

Use in HyphyLiquid:

- Keep ETH `eth_a_funding_context_follow` as the active v1 candidate.
- Do not re-open broad ETH fade/reclaim lanes without funding/OI context.
- Add predicted funding only as context; never as a direct entry trigger by itself.

### 3. Grid/Range Strategies Are Real, But Not V1 BTC/ETH Priority

Open-source and commercial Hyperliquid bots include grid/range strategies. Chainstack's open-source Hyperliquid bot ships a BTC grid preset; other platforms describe grid, DCA, and range capture on Hyperliquid. That validates Slim's instinct that sideways assets can be scalped or gridded.

Use in HyphyLiquid:

- Treat grid/range as the alt/Iron Eagle lane, not the BTC/ETH v1 execution lane.
- HYPE remains the best research candidate because its behavior can support wider movement pockets.
- DOGE/BNB/SOL need threshold and liquidity sanity before any grid interpretation.
- BTC/ETH grid can be researched later, but the current v1 edge is ETH funding-follow, not BTC grid.

### 4. Market Making Exists, But It Is a Different Risk Shape

Hyperliquid explicitly welcomes market making and public bot roundups discuss Hummingbot-style connectors. But market making requires inventory control, adverse-selection management, quote cancel/replace discipline, and fee/slippage math. It is not a quick rescue for the current liquidation bot.

Use in HyphyLiquid:

- Do not pivot v1 into market making.
- Borrow only the useful ideas: spread guards, inventory limits, order persistence checks, and maker/taker fee awareness.

### 5. AI Can Help, But Mostly at the Slow Control Layer

AI trading bot marketing points to regime detection, liquidation maps, whale/OI context, and adaptive parameters. That matches the best use case for HyphyLiquid: AI as a tape/regime/exit advisor, not as a sub-second execution engine.

Use in HyphyLiquid:

- AI may recommend `stand_down`, `maintain`, `tighten_stop`, `activate_trail`, `partial_exit`, or `paper_only`.
- AI must not widen stops, increase leverage, cancel stops, or double down automatically.
- AI decisions should run on 1m/5m/15m context packets, not tick-by-tick order-book changes.

## Strategy Families Ranked For HyphyLiquid

### A. Active Build: ETH Funding-Context Follow

Current internal read:

- Symbol: ETH
- Lane: side=A follow 60m, funding positive/elevated
- Direction: short-only
- Shape: 1m wait, 35 bps event-VWAP stop, no TP, 60m bot-managed timeout
- Latest canary/backtest read: n=60, PF 1.75, WR 61.67%, median +0.0791%, top-win share 8.52%

Why it fits:

- V1-eligible.
- Data-backed now.
- Matches external logic: funding/OI crowding plus liquidation flow.
- Can be represented through the current execution canary as a stop-only bracket plus bot-managed timeout.

Next build:

- Timeout exit supervisor.
- AI exit advisory packet.
- Paper-to-live reconciliation/audit.

### B. Watch Build: BTC B-Side Failed-Reclaim / Ask-Heavy

Why it stays watch:

- BTC has had positive pockets, but broad BTC edges decayed.
- L2/OBI/OFI did not rescue BTC across the latest Tier-2 pass.
- Needs n growth and live-like paper proof before front seat.

Next build:

- Do not tune more broad BTC.
- Watch only the narrowed BTC failed-reclaim/ask-heavy continuation pocket.
- Add AI chart/tape explanation when the deterministic pocket fires.

### C. Research Build: HYPE Range / Iron Eagle

Why it matters:

- External grid/range bot evidence supports the idea.
- Internal HYPE B-side/range pockets have shown potential but remain sample-thin and research-only.

Next build:

- HYPE wide/normal only.
- Kill compressed-band optimism.
- Test mid-band exit, partial mid-band exit, and trail remainder.
- No live promotion under current scope.

### D. Later Build: SOL/DOGE/BNB Threshold Sanity

Why it is not front seat:

- Sample sizes are still too small or thresholds may be mis-set.
- Grid/range viability depends on spread, depth, and event frequency.

Next build:

- Per-symbol threshold sanity.
- Spread/depth guard.
- Only then grid/range paper research.

### E. Not Now: Full Market Making / Arbitrage

Why not:

- Different bot class.
- More operational risk.
- Requires inventory and quote management that is not part of v1.

Borrow:

- fee-aware exits
- spread guards
- inventory caps
- maker/taker awareness

## Immediate Decision

Proceed with the working-bot path:

1. Build ETH timeout-exit supervisor.
2. Build AI exit advisory with hard fail-closed limits.
3. Keep BTC as watch.
4. Keep HYPE as research/Iron Eagle.
5. Repair MiniMax CLI separately; do not block the bot on it.

## MiniMax / Marvis Follow-Up Prompt

Use this after `mmx` is back on PATH:

> Research Hyperliquid BTC/ETH and HYPE bot strategy families that use liquidation flow, funding, open interest, grid/range logic, trailing stops, and AI/regime filters. Do not propose generic indicators alone. Return only concrete adjustment ideas for HyphyLiquid: entry context, exit logic, stop logic, risk failure mode, and whether it applies to ETH v1, BTC watch, or HYPE research. Flag anything that requires market making, cross-exchange arbitrage, or martingale as not v1.

## Sources

- Hyperliquid liquidations: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Hyperliquid funding: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- Hyperliquid market making: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/market-making
- Chainstack Hyperliquid grid bot repo: https://github.com/chainstacklabs/hyperliquid-trading-bot
- Chainstack Hyperliquid trading bot roundup: https://chainstack.com/hyperliquid-trading-bots-2026/
- Chainstack HIP-4 bot tutorial: https://chainstack.com/how-to-build-hip-4-trading-bot-on-hyperliquid/
- Hyperbot grid trading docs: https://docs.hyperbot.network/get-started/grid-trading
- OpenHyper strategy overview: https://openhyper.org/en/
- HyperPerps AI bot overview: https://hyperperps.app/

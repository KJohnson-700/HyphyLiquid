---
date: 2026-08-11
type: architecture-plan
project: hyphyliquid
status: active
---

# Live-Like Bot Architecture Plan

## Decision

HyphyLiquid should not be one giant trading loop. The safer architecture is a set of small workers with explicit contracts:

1. Data collectors
2. Feature builders
3. Deterministic strategy/router
4. Execution intent adapter
5. Position supervisor
6. AI advisory layer
7. Risk manager
8. Journal/audit/status workers

This matches the shape used by mature bot frameworks: exchange connectors/data providers feed strategy controllers, while separate executors manage order lifecycle and exits.

## Required Workers

### Data Collectors

Already mostly present.

- WebSocket trades/liquidations
- 1m candles
- BBO/L2 book snapshots
- active asset context: mark, OI, funding, predicted funding
- logs and health checks

Rule: use WebSockets for low-latency realtime data. Hyperliquid REST has rate limits; REST is for snapshots/backfill/status, not event-speed trading.

### Strategy Router

Owns:

- liquidation event classification
- ETH funding-context follow route
- BTC watch route
- HYPE/SOL/DOGE/BNB research-only routing

Does not own:

- live order placement
- timeout exits
- stop modification
- risk overrides

### Execution Intent Adapter

Owns:

- converting the active paper lane into `BracketOrderIntent`
- refusing retired/research lanes
- requiring ETH short stop-only shape for current v1

This is already started in `src/execution/paper_intents.py`.

### Position Supervisor

Owns:

- open position lifecycle
- max-hold timeout decisions
- reduce-only close intents
- future reconciliation between local state and Hyperliquid state

This was started on 2026-08-11 in `src/execution/position_supervisor.py`.

### AI Advisory

AI is a slow control layer, not the execution engine.

Allowed:

- `stand_down`
- `maintain`
- `tighten_stop`
- `activate_trail`
- `partial_exit`
- `paper_only`

Forbidden in v1:

- widen stop
- cancel stop
- increase leverage
- automatic double-down
- promote research-only symbols

Run cadence should be 1m/5m/15m packets, not tick-by-tick.

### Journal / Audit / Status

Every loop must leave artifacts:

- paper decision rows
- position rows
- fills/marks
- execution canary status
- paper audit
- vault changelog/research notes

This is how we avoid simulated/live drift becoming invisible.

## Latency Guidance

For v1 ETH funding-context follow, sub-millisecond latency is not the edge. The lane enters after a 1m wait and exits on stop or 60m timeout. The important latency problems are:

- WebSocket continuity
- clock sync
- stale snapshots
- reconnect/reconcile behavior
- order submission reliability
- avoiding REST rate-limit mistakes

A VPN is not automatically helpful. It can improve routing in some cases, but it can also add jitter, disconnects, and IP/rate-limit weirdness. Do not add VPN as a default architecture dependency. If latency becomes a measured problem, compare:

- local no-VPN ping/jitter to Hyperliquid API
- VPS no-VPN ping/jitter
- VPN path ping/jitter
- WebSocket reconnect frequency
- order ack timing

For now, a stable VPS close to good network routes is more useful than a VPN.

## Build Order

1. Position supervisor timeout close preview.
2. Live state reconciliation read: open position, open orders, stop order existence.
3. Reduce-only timeout close dry-run/testnet proof.
4. AI exit advisory packet, fail-closed.
5. Paper loop uses supervisor status.
6. Mainnet canary only after audit and double arm.

## Sources

- Hyperliquid rate limits and WebSocket limits: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- Hyperliquid exchange endpoint / order types: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- Hummingbot architecture and connectors: https://hummingbot.org/docs/
- Hummingbot executors: https://hummingbot.org/strategies/v2-strategies/executors/
- Freqtrade bot usage and monitoring: https://docs.freqtrade.io/en/latest/bot-usage/
- Freqtrade strategy anatomy: https://www.freqtrade.io/en/stable/strategy-101/

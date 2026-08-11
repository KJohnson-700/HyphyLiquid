---
date: 2026-08-10
type: plan
project: hyphyliquid
status: active
---

# AI-Integrated Bot Build Plan

## Why Marvis Got Confused

Cron will not make money. Cron is infrastructure. It keeps collectors, paper loops, audits, and canary checks running on time. The edge still comes from the strategy: liquidation event, funding/OI/book context, regime classification, risk control, execution quality, and disciplined exits.

The correct framing is:

liquidation/event data -> deterministic candidate lane -> AI regime/tape advisory -> deterministic validation -> risk.py -> OrderManager -> journal/audit.

AI helps with interpretation and playbook selection. It does not become the order manager.

## Current Strategy Reality

- Active v1 candidate: `ETH side=A follow 60m funding_z=funding_pos_elevated`.
- ETH execution shape: short-only, 1 minute wait, 35 bps event-VWAP stop, no TP on first pass, 60 minute bot-managed timeout exit.
- Latest ETH canary/backtest read: n=60, PF 1.75, WR 61.67%, median +0.0791%, top-win share 8.52%.
- BTC is secondary watch: `BTC side=B failed_reclaim_continuation + ask_heavy` has not earned the front seat.
- HYPE has the best alt research pockets, especially context/range states, but remains research-only.
- L2 depth/OBI/OFI did not rescue BTC/ETH: 0/240 passing buckets. Do not keep retuning that as if it is the answer.

## Where AI Helps Best

### 1. Tape and Regime Detection

AI reads a bounded packet containing:

- liquidation side and cascade clustering
- BBO spread and imbalance
- trade-flow imbalance
- 1m/5m/15m momentum
- realized volatility and Bollinger/ATR state
- funding and OI context
- recent paper/canary status
- current risk posture

AI output is limited to:

- `stand_down`
- `maintain`
- `paper_only`
- `watch_playbook`

The main value is recognizing disagreement, such as funding says continuation but tape is absorbing, or momentum says trend while liquidation clusters look exhausted.

### 2. Marginal Setup Review

AI should be called only when the deterministic bot has a candidate but evidence is mixed. It should not score every market tick.

Useful cases:

- ETH funding-follow signal appears, but spread/vol/news context is abnormal.
- BTC watch pocket appears, but recent BTC behavior has been decaying.
- HYPE research setup appears in wide/normal range and we want a research-only interpretation.

### 3. Chart Context

AI can summarize chart state from computed indicators, not raw vibes:

- compression vs expansion
- trend vs range
- liquidation event relative to event VWAP
- distance from Bollinger bands and ATR
- whether the move is reclaiming or failing to reclaim

The chart module should produce numbers and small image snapshots later; AI should explain them and flag conflicts.

### 4. News and Exchange Context

Daily/news cron is useful as context, not a trigger.

Examples:

- major CPI/FOMC day
- Hyperliquid incident or degraded API status
- large exchange outage
- ETF or regulatory event affecting BTC/ETH
- abnormal funding/news combination

AI may recommend `stand_down` or `paper_only` when news risk makes historical tape less trustworthy.

### 5. Post-Trade Review

AI can review closed trades and classify failure:

- stop too tight
- timeout too early
- no follow-through
- funding context wrong
- tape reversed
- execution/slippage issue

These reviews become tags. Only repeated tags become deterministic code changes.

## Where AI Must Not Help

- AI does not place orders.
- AI does not change leverage.
- AI does not widen stops mid-trade.
- AI does not promote HYPE/SOL/DOGE/BNB/metals into live execution.
- AI does not override `risk.py`.
- AI does not bypass the live arming guard.

## Build Order

### Phase 1 - Fix Advisory Contract

Update the AI advisory schema to know the current active playbooks:

- `eth_a_funding_context_follow`
- `btc_b_failed_reclaim_ask_heavy`
- `hype_b_range_scalp_research`
- `hype_b_wide_range_grid_research`
- `alts_collect_only`

The old `eth_rejected_collect_only` state is no longer the primary ETH story. Keep it only for rejected/reclaim ETH contexts.

### Phase 2 - Current Packet Builder

Build one packet per symbol with:

- latest deterministic route
- latest execution canary preview
- latest paper audit
- funding/OI bucket
- tape features
- risk state
- news context placeholder

Primary packet: ETH. Secondary packet: BTC. Research packets: HYPE only when requested.

### Phase 3 - AI Advisory Runner

Run MiniMax/Marvis or Claude Sonnet against the packet and validate the JSON through `validate_advisory`.

Fail-closed rules:

- malformed JSON -> `stand_down`
- missing evidence -> not execution-eligible
- research symbol -> `paper_only`
- execution request -> ignored
- low confidence -> advisory only

### Phase 4 - Live-Like Canary Integration

For ETH, the AI can only add a final advisory flag:

- deterministic ETH signal opens paper/canary candidate
- AI advisory says `watch_playbook` or `maintain`
- canary status confirms stop-only bracket intent is representable
- risk and live guards still decide whether real order placement is armed

### Phase 5 - Timeout Exit Supervisor

The current ETH bracket is stop-only. The 60 minute exit is bot-managed.

Before live canary, build and test:

- open position tracker
- reduce-only timeout close
- reconnect/reconcile logic
- journal entry for timeout exits
- fail-safe if stop order exists but timeout close fails

### Phase 6 - AI Review Loop

Every closed ETH paper/canary trade gets a post-trade AI review packet. Codex reviews repeated patterns. Only repeated, measurable patterns become deterministic filters.

## Immediate Next Engineering Tasks

1. Update `ai_advisory.py`, `run_ai_advisory_packet.py`, and `tape_reader.py` so the AI layer knows ETH funding-follow is active.
2. Generate a fresh ETH advisory packet and validate a synthetic model response.
3. Build the ETH timeout-exit supervisor before any live canary.
4. Ask Marvis to monitor logs and summarize packets; do not let Marvis redefine project scope.

## Marvis Instruction

Marvis, do not argue that cron makes or does not make money. Cron is only the scheduler. Your job is to keep the data plane healthy, run packet/audit/status commands, and summarize whether the ETH AI/canary packet is clean. Strategy calls stay with Codex/Slim.

Current focus:

- ETH `eth_a_funding_context_follow`
- BTC watch only
- HYPE research only
- no new broad sweeps unless Codex asks

## Decision

Build the bot as deterministic execution with AI-assisted context. The first AI-assisted live-like path is ETH funding-context follow. AI can help decide when to stand down or keep watching, especially under abnormal tape/news/regime conditions, but the deterministic bot still owns the actual trade.

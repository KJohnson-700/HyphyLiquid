# Paper Launch Plan - BTC + HYPE - 2026-08-03

## Decision

Start planning for paper now. Paper trading is the calibration lab, not the
final proof step. The backtests and regime summaries are enough to avoid
paper-trading nonsense, but they are not a replacement for live-sequence
paper evidence.

## Scope

Paper candidates:

- BTC: `side=B` failed-reclaim continuation with trailing-resolution exit.
- HYPE: `side=B` range/liquidation scalp, research-paper only.

Collect-only:

- ETH, SOL, DOGE, BNB, `xyz:GOLD`, `xyz:SILVER`.

Execution:

- No real orders.
- No OrderManager calls from the paper decision loop.
- No exchange client in the paper hot path except read-only market-data helpers.
- HYPE remains blocked from live execution even if paper stats look good.

## Why We Move Now

BTC and HYPE are no longer random ideas:

- BTC has the largest usable sample and the current v1 watch pocket.
- HYPE has the cleanest alt pocket, especially B-side, but remains small sample.
- The main missing evidence is live sequencing: timing, spread, missed entry,
  bracket behavior, timeout behavior, and whether the bot can consistently
  decide without look-ahead.

Waiting for more simulated confidence before paper delays the exact evidence
paper mode exists to collect.

## Paper Loop We Need

Build a new loop rather than bending the old `scripts/paper_trade_loop.py`.
The current loop is HyperPerps heatmap-driven for BTC/ETH/SOL. It does not
paper the exact BTC/HYPE liquidation-regime lanes.

Target new script:

```text
scripts/paper_decision_loop.py
```

Target output:

```text
data/paper_decisions_YYYYMMDD.jsonl
data/paper_positions_YYYYMMDD.jsonl
```

## Live-Like Paper Requirements

Paper must stay as close to live as possible so the transition does not create
a new failure mode.

Use the same live-path inputs:

- Same WebSocket-derived liquidation events.
- Same 1m candle stream.
- Same event VWAP.
- Same book/BBO snapshots where available.
- Same asset-ctx OI/funding snapshots.
- Same regime router.
- Same risk limits, position caps, and allowlist logic.

Use a paper broker only at the final order/fill boundary:

- The paper loop may not call `OrderManager.execute()`.
- The paper loop may not instantiate a real exchange order client.
- The simulated order object should mirror the live bracket shape:
  entry, stop, target/trailing fields, reduce-only exit intent, and client ID.
- Paper fill rules should be conservative:
  - stop beats target when both touch in the same candle,
  - marketable stop exits include slippage,
  - entries can miss if the next candle never trades through the simulated
    entry price,
  - fees/spread/slippage are subtracted from every result.
- Paper should log both "ideal close-based" and "live-like conservative" PnL
  so drift is visible instead of hidden.

Every live promotion review must compare:

- backtest expected result,
- paper ideal result,
- paper live-like result,
- missed-entry count,
- stop-slip count,
- timeout count,
- latency from event to decision.

Core behavior:

1. Read fresh liquidation/cascade/event-feature state.
2. Load latest 1m candles for BTC and HYPE.
3. Classify candle regime and liquidation response with `src.strategy.regime`.
4. Route:
   - BTC route `watch` -> paper candidate.
   - HYPE route `research_candidate` -> research-paper candidate.
   - everything else -> logged skip.
5. Create a simulated bracket:
   - BTC: event-VWAP invalidation stop plus trailing activation/trail settings.
   - HYPE: mid-band target and outer-band stop plus buffer.
6. Update open paper positions on each new 1m candle.
7. Append every decision and every paper fill/exit. Never overwrite.

## Required Log Fields

Each decision row should include:

- `decision_ts`
- `lane`
- `symbol`
- `side`
- `action`: `enter`, `skip`, `exit`, `update`
- `route_action`
- `execution_allowed`
- `paper_scope`: `v1_paper` or `research_paper`
- `regime`
- `response`
- `event_ts`
- `event_vwap`
- `entry_price`
- `stop_price`
- `target_price` or trailing fields
- `max_hold_minutes`
- `reason`
- `config`

Each position row should include:

- `paper_order_id`
- `opened_ts`
- `closed_ts`
- `symbol`
- `lane`
- `direction`
- `entry_price`
- `exit_price`
- `exit_reason`
- `gross_return_pct`
- `net_return_pct`
- `r_multiple`
- `bars_held`
- `mae_pct`
- `mfe_pct`

## Initial Paper Settings

BTC:

- Symbol: BTC only.
- Side: B only.
- Variant: failed-reclaim continuation.
- Stop: event-VWAP invalidation with current best-tested buffer family.
- Trailing: activation around 2R, trail around 10 bps.
- Max hold: 120-240m family tracked separately.
- Goal: confirm live sequencing and stop/trailing behavior.

HYPE:

- Symbol: HYPE only.
- Side: B only.
- Regime: `range_normal` and `range_wide` only.
- Reject: `range_compressed`.
- Target: mid-band.
- Stop: outside outer band plus buffer.
- Max hold: 15m initial family, with 5/30m tracked as alternates later.
- Goal: gather research-paper evidence without touching v1 execution.

## Promotion / Demotion

Paper start does not mean live promotion.

BTC can move from paper to live-canary discussion only after:

- Enough paper trades to reveal execution behavior.
- No missed protective-exit logic.
- Median after costs remains positive.
- PF remains above threshold after costs.
- Drawdown and stop behavior match risk limits.
- OrderManager testnet bracket proof is clean.

HYPE can move from research-paper to v1 discussion only after:

- The sample is no longer tiny.
- B-side range-normal/wide survives paper.
- Compressed bucket remains rejected.
- Slim explicitly changes scope, because HYPE is research-only today.

Immediate demotion triggers:

- Any paper loop tries to instantiate a real exchange/order client.
- Any non-BTC/HYPE candidate creates a paper entry.
- HYPE `range_compressed` creates a paper entry.
- BTC A-side or ETH creates a paper entry.
- Exit updates require look-ahead candles.

## Role Split

Codex:

- Owns the paper-lane spec.
- Owns deterministic routing and promotion/demotion decisions.
- Reviews any implementation that touches execution boundaries.

Claude Code / top-level coding:

- Build `paper_decision_loop.py` if the implementation becomes broad.
- Add focused tests for no-exchange boundary, BTC/HYPE-only paper entries,
  HYPE compressed rejection, and append-only logs.

Marvis:

- Keep collectors running.
- Run rebuild/status checks.
- After each rebuild, run regime summary and trailing stability.
- Gather paper log summaries once the paper loop is live.
- Do not tune settings or propose live promotion without Codex review.

## Next Build Steps

1. Add a small paper ledger module with append-only JSONL helpers.
2. Add a no-exchange `PaperPosition` lifecycle simulator using existing exit
   math where possible.
3. Add `scripts/paper_decision_loop.py` as a separate process from the old
   HyperPerps loop.
4. Add status output for BTC/HYPE paper lane counts and last decision.
5. Run in foreground for one cycle, then daemonize only after logs look right.

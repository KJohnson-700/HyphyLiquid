# Regime Map Handoff - 2026-08-03

## Scope

Codex added the first deterministic regime map for HyphyLiquid. This is the
bridge between raw liquidation events and an AI-assisted analyst layer:
models can help summarize and propose research, but execution gates stay
hard-coded and replayable.

No live or paper execution behavior changed.

## Code

- `src/strategy/regime.py`
- `tests/test_regime.py`

The module labels:

- Candle regime from prior completed 1m candles:
  - `range_compressed`
  - `range_normal`
  - `range_wide`
  - `range_very_wide`
  - `trend_up`
  - `trend_down`
  - `high_vol_cascade`
  - `no_data`
- Liquidation response:
  - `post_liquidation_reclaim`
  - `post_liquidation_continuation`
  - `unknown`
- Asset route:
  - `watch`
  - `research_candidate`
  - `collect_only`
  - `reject`

## Current Routing Read

BTC:

- Current v1 watch pocket is BTC `side=B` failed-reclaim continuation.
- It routes to `btc_eth_trailing_resolution` as `watch`, not auto-trade.
- This matches the trailing stability note: promising in mature subset, not
  confirmed by broad coverage yet.

ETH:

- Routes to `reject` under current fade/follow and trailing framing.
- ETH can be revisited only if a new framing survives focused tests.

HYPE:

- `side=B` plus `range_normal` or `range_wide` routes to
  `research_candidate`.
- `range_compressed` routes to `reject` because current diagnostics show the
  compressed bucket losing.
- HYPE remains research-only and cannot feed execution.

SOL/DOGE/BNB:

- Route to `collect_only` until sample size and diagnostics justify deeper
  study.

## Marvis Delegation

After each mature rebuild, Marvis should collect regime evidence and hand it
back without interpretation or execution advice:

1. Rebuild metadata: commit hash, UTC timestamp, cascade counts by symbol and
   side.
2. Per-symbol regime counts: candle regime, liquidation response, and route
   action.
3. BTC watch pocket: count BTC `side=B` continuation events, trailing result,
   activation rate, initial-stop rate, median return, PF.
4. ETH rejected lane: confirm no tested ETH bucket crossed the promotion gate.
5. HYPE research pocket: split B-side results by `range_normal`, `range_wide`,
   and `range_compressed`.
6. Safety gate check: verify SOL/HYPE/DOGE/BNB remain `execution_allowed=false`.
7. Append results to a timestamped research file; do not overwrite old rows.

## Promotion Guard

Do not promote a route because the label exists. Promotion still needs:

- n>=100 for BTC/ETH execution candidates.
- Positive median after costs.
- PF >1.5 after costs across broad and mature samples.
- No single-trade outlier concentration carrying the result.
- Stop/exit behavior that survives stability checks.
- Risk module and OrderManager gates unchanged.

## Verification

Focused tests passed:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe -m unittest tests.test_regime tests.test_lane_backtest tests.test_trailing_stability_report
```

Result: 28 tests passed.

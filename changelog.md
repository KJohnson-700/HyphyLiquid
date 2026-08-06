# HyphyLiquid — Changelog

> Running log of meaningful changes, fixes, and milestones. Most recent first.

---

## 2026-08-06 - Add execution canary and paper-to-live bracket intent

Codex moved the build path from research-only loops toward tomorrow's execution-grade canary.

**What changed**
- `src/execution/order_manager.py` now has `BracketOrderIntent` plus `OrderManager.execute_bracket_intent()`, so a deterministic paper lane can hand the live order manager the exact entry/stop/target bracket instead of re-deriving TP/SL from ATR.
- The new bracket-intent path still enforces the v1 allowlist, stop geometry, position rounding, leverage cap, risk-per-trade cap, reduce-only child orders, and `normalTpsl` grouping.
- `scripts/run_execution_canary.py` is the new one-command canary runner. Paper mode runs `paper_decision_loop`, runs the paper audit, and writes `data/execution_canary_status.json` plus `.md`.
- Live mode is double-armed: `HYPHYLIQUID_LIVE_TRADING_ENABLED=1` and `--i-understand-real-orders` are both required, and `HYPERLIQUID_ENV` must be `mainnet`.
- `.env.example` documents the live arming flag.
- Added tests for the execution-canary live guard and bracket-intent order-manager path.

**First run**
- `scripts/run_execution_canary.py --mode paper --max-new 250 --recent 12` completed.
- Paper pass: 155 decisions written, 1 position opened, 1 closed, 0 audit anomalies.
- Current paper performance remains negative; this change is plumbing/safety, not a strategy promotion.

**Verification**
- `python -m py_compile scripts/run_execution_canary.py tests/test_execution_canary.py src/execution/order_manager.py tests/test_order_manager.py`
- `python -m unittest tests.test_execution_canary -v` -> 5/5 passing
- Dependency-free bracket-intent smoke script -> OK
- Full `tests.test_order_manager` could not run in this session because the fallback Python lacks `pandas` and the repo venv points at a missing Windows Store Python.

---

## 2026-08-06 - BTC ask-heavy × failed-reclaim JOIN backtest + ETH paper/research lane

Per Slim 2026-08-06: "BTC ask-heavy needs a more specific failed-reclaim join, not generic book persistence. ETH just earned a focused paper/research lane."

**What changed**
- New `scripts/run_btc_ask_heavy_reclaim_join.py` (~30KB) — per-event BTC/ETH backtest of the JOIN: cascade-time BBO ask_heavy AND standard failed_reclaim_continuation. Compares against `generic_failed_reclaim_continuation` (control) and sister buckets (`ask_heavy_AND_always_fade`, `ask_heavy_AND_always_follow`, `bid_heavy_AND_failed_reclaim_continuation`) so the value of the join is visible
- New `tests/test_btc_ask_heavy_reclaim_join.py` — 34/34 tests passing. Covers: BBO bucket classification, reclaim detection, entry/exit extraction, trade-record PnL math (cost in bps), promotion gate, per-bucket aggregation, per-cascade processor populates the right buckets, end-to-end main() on synthetic data
- New `data/btc_ask_heavy_reclaim_join_results.json` + `_summary.md` — first honest read on real data
- `scripts/run_rebuild_cycle.py` — added `BTC_ASK_HEAVY_RECLAIM_JOIN_CMD` as 16th best-effort command (post-cycle, doesn't block baseline)
- `src/strategy/regime.py` — added `eth_book_persistence_fade` lane. ETH B-side failed-reclaim continuation is now a `research_candidate` (paper-routable, NOT live). ETH B-side with reclaim is `watch` (paper logs only). ETH A-side is `collect_only`. `execution_allowed=False` for all ETH paths — OrderManager v1 is still BTC/ETH execution scope, but ETH paper is research-only
- `scripts/paper_decision_loop.py`:
  - `PAPER_SYMBOLS` now `{"BTC", "ETH", "HYPE"}` (was `{"BTC", "HYPE"}`)
  - New constants: `ETH_REQUIRED_IMBALANCE_BUCKET="ask_heavy"`, `ETH_MAX_HOLD_MINUTES=5`, `ETH_STOP_BUFFER_BPS=20.0`, `ETH_WAIT_MINUTES=1`
  - New `_build_eth_position` function: ETH B-side cascade + BBO ask_heavy + no reclaim (1m wait) → enter long at wait bar, stop at event_vwap - 20bps, 5m horizon, no trail
  - `build_position_for_cascade` routes ETH to `_build_eth_position`
  - Paper scope is `research_paper` (vs BTC's `v1_paper`); execution is OFF (`execution_allowed=False`)
- `tests/test_paper_decision_loop.py`:
  - Updated `test_run_once_opens_and_marks_btc_and_hype_only` → `test_run_once_opens_and_marks_btc_eth_and_hype` (now exercises ETH in PAPER_SYMBOLS)
  - New `TestEthPaperLane` class with 7 tests covering: ETH in PAPER_SYMBOLS, constants documented, B-side continuation opens research_paper position, ask_heavy gate rejects non-ask-heavy, reclaim in wait window rejects, A-side cascades rejected, bracket sizing uses risk module
- `tests/test_regime.py`:
  - Replaced `test_eth_rejects_until_new_framing_passes` with three new tests: `test_eth_b_side_continuation_is_research_candidate_per_slim_2026_08_06`, `test_eth_b_side_reclaim_watch_only`, `test_eth_a_side_collect_only`

**First honest read on real data (2026-08-06)**
- BTC ask-heavy × failed-reclaim JOIN: **all buckets fail the promotion gate**. BTC B ask_heavy_AND_failed_reclaim_continuation: n=58, WR 20.7%, PF **0.27**, med -0.0929%, top 21.1%
- vs BTC B generic_failed_reclaim_continuation (control): n=97, PF 0.16 — the join is slightly less negative than generic, but both fail; both have negative median returns
- BTC ask_heavy alone (always_fade): n=211, PF 0.14. always_follow: n=211, PF 0.28. BBO filter alone is not enough
- BTC bid_heavy × failed_reclaim: n=36, PF 0.03 (terrible)
- ETH B ask_heavy × failed_reclaim: n=54, PF 0.26 (also negative)
- **Per the standing rule, do not propose a re-tune.** The BBO filter adds a tiny absolute uplift to BTC B (PF 0.16 → 0.27) but both are below 1.5 and both are negative. The decay continues. Don't keep re-evaluating simple rules on more data without a new filter/feature.

**Test count after this commit**: 437/437 passing (was 394 before). Net delta: +43 (34 BTC ask-heavy + 7 ETH lane + 1 paper loop update + 1 regime update).

**Hard scope held**: zero changes to execution, order_manager, risk.py, or live/paper routing beyond the new ETH research-paper lane (which is paper-only by design — no OrderManager touch).

---

## 2026-08-06 - Fix rebuild bottleneck and ETH paper proxy gate

Codex fixed two issues found during the forced rebuild cycle.

**What changed**
- `scripts/build_cascades.py` now enriches cascades one symbol-date group at a time instead of loading every L2/context index at once. This keeps memory bounded after BTC/ETH/SOL/HYPE/DOGE/BNB/HIP-3 data growth.
- Full all-symbol enriched cascade build completed successfully after the fix: 29,367 raw events -> 3,125 cascades.
- `scripts/paper_decision_loop.py` now requires the actual ETH `flow_amplifies_30s` condition for the `eth_book_persistence_fade` research-paper lane instead of using cascade-time `ask_heavy` as a proxy.
- Added/updated unit coverage in `tests/test_paper_decision_loop.py` for ETH trade-flow gating.

**Why**
- The forced rebuild timed out twice before the grouped-enrichment fix.
- Fresh paper audit showed the ETH `ask_heavy` proxy was losing: research-paper ETH n=19, PF 0.1021. That proxy did not match the passing backtest condition, so it is now blocked unless 30s post-event trade flow actually amplifies the cascade.

**Verification**
- `python -m py_compile scripts/build_cascades.py scripts/paper_decision_loop.py tests/test_paper_decision_loop.py`
- `python -m unittest tests.test_paper_decision_loop -v` -> 16/16 passing
- `scripts/run_rebuild_cycle.py --force` completed and updated baseline.

---

## 2026-08-06 - Add book-persistence / stale-book / trade-flow filter backtest

Per Slim's 2026-08-06 spec. BTC/ETH only. Research-only backtest that adds three new context filters to the cascade-fade playbook. Hard scope: no execution, no risk.py, no order_manager changes.

**What changed**
- New `scripts/run_book_persistence_filter.py` (~38KB) — per-event feature assembler + per-bucket stats against the standard promotion gate
- New `tests/test_book_persistence_filter.py` — 49/49 tests passing. Covers: BBO/L2/trade parsers, imbalance math, persistence-counting (consecutive same-direction BBO over 5m window), stale-book flag (spread widened >1.5x AND mid drift <0.01%), trade-flow stats (30s/60s windows), flow-amplifies/fades labels (side-aware), book-absorbed/amplified labels, promotion gate, full per-event feature assembly smoke test
- Binary-search window helpers (`_trades_in_window`, `_bbo_window`, `_bbo_imbalance_series`) so per-event feature work is O(log N) instead of O(N) over 2M+ BBO snapshots
- Bucket key: `(scope, symbol, lane, paper_gate)` reuses same bucketing pattern as the current-gate paper audit

**Features computed per cascade**
- BBO imbalance at event + bucket (bid_heavy / balanced / ask_heavy)
- L2 top-5 imbalance at event + bucket
- BBO persistence: consecutive same-direction imbalance in [event-5m, event], threshold 30s
- Stale book: spread widened >1.5x 5m median AND mid drift <0.01%
- Post-event BBO drift at 30s/60s (book absorbed vs amplified)
- Trade-flow imbalance at 30s/60s (amplifies vs fades the cascade)

**Playbook filters tested**
- `generic` (control)
- `persistent_bid_heavy` / `persistent_ask_heavy`
- `stale_book`
- `flow_amplifies_30s` / `flow_amplifies_60s`
- `flow_fades_30s` / `flow_fades_60s`
- `book_absorbed_30s` / `book_amplified_30s`
- `persistent_bid_heavy_AND_flow_fades_30s` (combined)

**Real data read (2026-08-06, 1305 BTC/ETH cascades)**
- 1224 per-event records computed, 81 skipped (no entry/exit coverage)
- **Two passes** (n>=30, PF>1.5, med>0, top_win_share<=35%):
  - **ETH flow_amplifies_30s 5m**: n=30, WR 56.7%, PF **2.01**, med +0.0268%, top 12.2%
  - **ETH flow_amplifies_60s 15m**: n=39, PF **1.62**, med +0.0581%, top 20.1%
- BTC generic: PF 0.93-1.00 across horizons (decay continues, matches prior backtest cycles)
- BTC book_absorbed_30s 60m: n=172, PF 1.36, med +0.0360% (close to threshold, doesn't pass PF>1.5)
- BTC flow_amplifies_*: PF 0.11-0.35, much worse than ETH (PF 1.31-2.01) — flow direction matters differently per symbol
- ETH persistent_bid_heavy / ask_heavy: n=12-13 (too small to evaluate)

**Hard scope held**
- Zero changes to execution, order_manager, risk.py
- Outputs are research-only: `data/book_persistence_filter_results.json` + `data/book_persistence_filter_summary.md`
- Cycle did not change (research script is one-off, not wired into the rebuild cycle)

---

## 2026-08-06 - Losing-lane rescue experiment sweep

Codex used MiniMax CLI plus external source checks to refocus strategy research from new-bot ideas to concrete rescue experiments for weak lanes. Vault note: `research/2026-08-06-LOSING-LANE-RESCUE-EXPERIMENTS.md`.

**Decision:** next research should test context filters and failure-mode diagnostics: BTC ask-heavy persistence/stale-book audit, ETH balanced-reclaim near-miss filter, HYPE wide-bucket isolation, DOGE/BNB threshold sanity, SOL adaptive dislocation, and HIP-3 metals funding/basis probe. No execution/risk/order-manager changes.

---

## 2026-08-05 - Add current-gate-only paper audit section

Per Slim's 2026-08-05 spec. New section in `scripts/run_paper_audit.py` that separates legacy paper trades from current active gated paper trades. Hard scope: research/audit only. Does NOT touch execution, order_manager, risk.py.

**What changed**
- New `_current_gate_records(position_rows)`: filters to rows with `metadata.paper_gate` set (non-empty string). Excludes legacy (no gate) and research_paper scope (no v1 gate).
- New `_summarize_current_gate_only(position_rows)`: per-bucket (scope, symbol, lane, gate) counts of n, WR, PF, avg/median net return, avg/median R, exit reasons. BTC `ask_heavy` highlighted as a separate aggregate.
- New `## Current Gate Only` section in `render_markdown` (above the Top Reject Reasons section, below By Symbol). Three sub-sections: gated/non-gated counts, BTC ask_heavy aggregate, by-bucket breakdown.
- `main()` JSON output now includes `gated_closed` and `gated_btc_ask_heavy_n` for quick cycle summaries.
- Existing full-history audit (`decision_summary`, `fill_summary`, `anomalies`, `legacy_warnings`, `recent`) is **preserved** — new section sits alongside, not replacing.

**Tests added (4 new, 7 total in this file)**
- `test_current_gate_only_separates_gated_from_legacy`: 1 BTC win + 1 BTC loss + 1 HYPE legacy. Verifies gated vs non-gated split, bucket key, WR/PF math, exit-reason counts, BTC ask_heavy highlight, and that the markdown section renders.
- `test_current_gate_only_empty_when_no_gates`: dataset with only non-gated positions returns empty aggregates (no false positives).
- `test_current_gate_records_filters_correctly`: tests the filter against empty/None/non-dict/missing-key edge cases.
- `test_gate_bucket_key_format`: locks the bucket key tuple shape.

**Real data read (2026-08-05)**
- 25 closed fills total, 22 are legacy BTC (predate the ask_heavy gate), 1 BTC has the current ask_heavy gate (lost, initial stop), 1 HYPE has no v1 gate (research_paper).
- BTC ask_heavy aggregate: n=1, WR=0%, PF=0, net=-0.31%, R=-1.49. Single trade, not interpretable yet.
- 459 non-gated records (legacy + research_paper HYPE/SOL/etc).
- The new section surfaces the single gated BTC trade alongside the 22 legacy warnings, so we can see the migration from legacy to current-gate going forward.

---

## 2026-08-05 - MiniMax hybrid strategy candidate sweep

Codex used MiniMax CLI for a broad strategy synthesis pass and logged the filtered build order in the vault at `research/2026-08-05-HYBRID-STRATEGY-CANDIDATES-MINIMAX-SWEEP.md`. Decision: do not add a new live bot or promote alts; prioritize stricter hybrid classifiers around the existing liquidation trigger.

---

## 2026-08-05 - SOL H1 watch: two-tier ladder (2 watch / 3 paper-routing)

Per Slim's 2026-08-05 refinement to the SOL H1 decision rule. Research-only. No execution wiring.

**Slim's three knobs (locked)**
- Calm threshold: BTC <= 5 bps/min stdev, ETH <= 8 bps/min stdev (kept as-is; do NOT tighten to 3/5 to recover the pass)
- Watch threshold: 2 consecutive cycles -> `watch-tracking`
- Paper-routing threshold: 3 consecutive cycles + paper sim wired -> `watch-confirmed`

**Why the extra tier**
Slim's call: the signal is interesting but not yet proven. With the fixed 5/8 bps thresholds, SOL H1 30m PF is 1.10 and 60m PF is 1.19, both below the 1.5 gate. The earlier 1.60/1.64 was threshold-sensitive (the run-median was 3.9 bps, tighter). Tightening to 3/5 to recover the pass would be curve-fitting — the guard did its job.

**What changed**
- Added `N_PAPER_ROUTING_CYCLES = 3` constant. New status tier `watch-tracking` between `watch-pending` and `watch-confirmed`.
- Extracted pure `_derive_watch_status(consecutive, paper_ready, ...)` function. Two-tier ladder is now testable in isolation.
- CLI flag `--paper-routing-cycles` to override the new threshold.
- Decision rule logged in the watch record now includes both `cycles_required` and `paper_routing_cycles`.

**Status flow (final)**
- consecutive < 2         -> watch-pending       (gathering)
- 2 <= consecutive < 3    -> watch-tracking      (on watch, paper-routing not yet a discussion)
- consecutive >= 3, no paper sim  -> watch-pending-paper
- consecutive >= 3, paper sim OK  -> watch-confirmed

**Codex next task (per Slim)**
Wire `paper_decision_loop.py` to tag decisions with `playbook: "sol_h1"` so the watch can detect paper sim readiness. Research-only; OrderManager stays blocked from execution.

**Tests**
- 9 new status-logic tests (covers all four status tiers + custom thresholds)
- 78/78 tests passing across the two new test files

**First read after refinement**
- consecutive_passes=0, cumulative_passes=0
- status=`watch-pending` (same as before — no new data has been collected)
- BTC: n=528, PF 0.76. HYPE: n=22, PF 0.82.

---

## 2026-08-05 - SOL H1 strict-decision-rule watch (research only)

Per Slim's 2026-08-05 spec. Research-only watch tracking SOL H1 across rebuild cycles with a fixed calm definition, repeated-confirmation rule, and paper-sim requirement before any scope discussion. Zero execution wiring.

**What changed**
- Refactored `scripts/run_relative_value_dislocation.py`:
  - Added `FIXED_CALM_VOL_BTC_30M = 0.0005` (5 bps/min stdev) and `FIXED_CALM_VOL_ETH_30M = 0.0008` (8 bps/min stdev) constants.
  - `_calm_vol_threshold()` now returns the fixed values; `--calm-vol-btc` / `--calm-vol-eth` CLI flags allow override for exploration.
  - JSON output adds `btc_calm_vol_threshold` / `eth_calm_vol_threshold` (replacing the prior `btc_vol_30m_median` / `eth_vol_30m_median`).
  - Test count: 47 (was 45; added two fixed-threshold tests).
- Added `scripts/check_sol_h1_watch.py` (SOL H1 watch, BTC/HYPE observation, paper sim status, watch log append).
- Added `tests/test_check_sol_h1_watch.py` — 22/22 tests passing.
- Wired `check_sol_h1_watch.py` into `scripts/run_rebuild_cycle.py` as the 14th command (best-effort, never blocks baseline).

**Decision rule (per Slim, strict)**
- 2 consecutive cycles where BOTH 30m and 60m SOL H1 verdicts pass the standard promotion gate (n>=30, PF>1.5, med>0, top_win_share<=35%)
- AND >= 5 paper decisions tagged with the SOL H1 setup
- Until paper sim is H1-aware, watch status is `watch-pending-paper` (not `watch-confirmed`)

**First read**
- SOL H1 30m: n=117, PF 1.10, med +0.0014% (fails PF<=1.5 with the fixed 5 bps BTC / 8 bps ETH threshold; was PF 1.60 with the run-median 0.000394)
- SOL H1 60m: n=117, PF 1.19, med +0.0068% (was 1.64)
- The signal is sensitive to the threshold. The fixed 5/8 bps may be too loose; run-median was tighter. Slim's call on which is the right number.
- Watch status: `watch-pending`. BTC: n=528, PF 0.76. HYPE: n=22, PF 0.82.

---

## 2026-08-05 - Clarify SOL H1 fixed calm threshold wording

Codex cleaned stale wording in `scripts/run_relative_value_dislocation.py` so the generated H1 explanation says fixed calm thresholds, not the old run-median rule. No strategy behavior changed.

---

## 2026-08-05 - Relative-value / dislocation backtest (research only)

Per Slim's 2026-08-05 spec. Research-only. No execution, no order_manager, no risk.py, no live/paper routing touched.

**What changed**
- Added `scripts/run_relative_value_dislocation.py` — per-event BTC/ETH-relative value backtest with rolling beta, deviation, funding/OI context, calm/confirm/isolation playbooks.
- Added `tests/test_relative_value_dislocation.py` — 45/45 tests passing. Covers forward-return math, fade direction, rolling beta, realized vol, deviation, funding/OI buckets, top_win_share, confirm logic, and all four promotion-gate failure modes.
- Outputs `data/relative_value_dislocation_results.json` (38KB) and `data/relative_value_dislocation_summary.md` (10KB).

**Per-symbol first read (481 events evaluated, 484 cascades total)**
- SOL generic: PF 0.81-1.33 across horizons (continues decay).
- SOL H1 (BTC/ETH calm): **PASS at 30m (n=62, PF 1.60) and 60m (n=62, PF 1.64)**.
- DOGE/BNB: n<30, no signal.
- xyz:SILVER: generic 5m PF 1.38 (n=173) is closest to threshold.
- xyz:GOLD: fails everywhere under BTC/ETH context — prior simple-fade PF 1.90 likely contaminated by majors.
- H3 isolated only fires for xyz:SILVER (n=3, too small).

**Verdict**
- SOL H1 is a new filter result (not a rehash of the simple rule). Re-run at next cycle; if it re-passes at n>=80, it becomes a candidate for Slim's filter conversation.
- No promotion of any alt symbol. v1 stays BTC/ETH-only.
- No execution wiring changes. This is data-layer work only.

---

## 2026-08-05 - Add research-only event range grid lane

Codex added a bounded grid-style research backtest for assets that are weak under the current cascade lanes.

**What changed**
- Added `src/strategy/grid_backtest.py` with a research-only event-anchored range grid.
- Added `scripts/run_grid_backtest.py` to test SOL/HYPE/DOGE/BNB and HIP-3 probes without touching execution.
- Added `tests/test_grid_backtest.py` for range-sweep entry, scope rejection, band-bucket rejection, and summary output.

**First read**
- HYPE side=B: n=20, PF 1.81, avg +0.0521%, median -0.0721%.
- HYPE side=B wide bucket: n=5, PF 5.03, median +0.1535%, sample too small.
- HYPE normal bucket remains weak: n=15, PF 1.17, median -0.1057%.
- DOGE/SOL/BNB/xyz:GOLD/xyz:SILVER are still too small or negative for action.
- Added `scripts/run_grid_sweep.py` for focused parameter sweeps.
- Focused HYPE side=B wide sweep best row: n=7, PF 2.01, avg +0.0770%, median -0.0529%, top_win_share 53.3%, watch_pass=False.

**Decision**
- Treat grid as research-only. It may help HYPE in wide range regimes, but the median problem and tiny wide-bucket sample mean no paper/live promotion yet.
- Run focused grid sweeps only for now; broad sweeps are slower and not justified until the pocket improves.

---

## 2026-08-05 - Add AI advisory guardrail contract

Codex added the first safe integration point for AI-assisted regime/tape review.

**What changed**
- Added `src/strategy/ai_advisory.py` with a fail-closed advisory contract.
- Added `scripts/run_ai_advisory_packet.py` to build bounded context packets from current diagnostics for an AI co-pilot.
- Added tests proving AI advice cannot place orders, cannot promote research symbols into execution, and must include regime/tape/risk evidence before becoming execution-eligible.
- Added `scripts/compare_ai_advisory_models.py` so Claude Sonnet vs Opus advisory outputs can be compared from the same packet.
- Generated `data/ai_advisory_packet_latest.json` for the current BTC context.

**Decision**
- AI can help drive attention, regime interpretation, playbook selection, and trade ideas for review.
- AI cannot bypass `risk.py`, cannot alter leverage/size/stops directly, and cannot execute orders.
- When testing Claude, try Sonnet first and compare against Opus; judge guardrail-clean evidence quality, not model prestige.

---

## 2026-08-05 - Add paper simulation audit report

Codex added a ledger-based paper audit so the live-like paper lane can be debugged from explainable decisions and fills.

**What changed**
- Added `scripts/run_paper_audit.py` to summarize paper decisions, opened positions, closed fills, exit reasons, PnL/R, recent opens, current anomalies, and legacy pre-gate warnings.
- Wired the audit into `scripts/run_rebuild_cycle.py` after the paper decision loop so every rebuild refreshes `data/paper_audit_latest.json` and `data/paper_audit_latest.md`.
- Fixed a broker edge case where a recovered position whose entry candle was not available could fail instead of staying safely open/unmarked.
- Added focused tests for the audit report and the missing-entry-candle broker path.

---

## 2026-08-04 - Add BTC/ETH filter diagnostics

Codex added the first deterministic filter readout for the BTC/ETH v1 lane.

**What changed**
- Added `src/strategy/filter_diagnostics.py` to join BTC/ETH lane trades back to enriched cascade features.
- Added `scripts/run_filter_diagnostics.py` to rank feature buckets by win rate, average/median return, PF, and outlier concentration.
- Wired the filter diagnostic into `scripts/run_rebuild_cycle.py` as a best-effort post-trailing step so each rebuild produces `data/btc_eth_filter_diagnostics.json`.
- Tightened the live-like BTC paper lane to only open the filtered BTC B-side failed-reclaim continuation candidate when the event top book is `ask_heavy`.
- Fixed paper-loop backlog scanning so heavy markets do not skip older unprocessed BTC/HYPE paper candidates when more than `max_new` cascades arrive between passes.
- Updated open paper marks to report unrealized P&L from the latest completed candle instead of always showing `0.0%` until close.
- Added focused unit tests for spread, imbalance, funding, size, staleness, enrichment, and min-sample behavior.

---

## 2026-08-04 - Fix HIP-3 xyz symbol wiring

Codex corrected the HIP-3 metal collector wiring so gold/silver can actually flow.

**What changed**
- Corrected metal symbols from `XYZ:GOLD`/`XYZ:SILVER` to Hyperliquid's live `xyz:GOLD`/`xyz:SILVER` names.
- Added Windows-safe metal file handling (`xyz_gold_*`, `xyz_silver_*`) while preserving canonical in-memory symbols.
- Updated liquidation monitor filename parsing so HIP-3 trade files produce correctly labeled liquidation events.
- Fixed rebuild enrichment to index snapshot files by each cascade's real event date instead of the stale hardcoded 2026-08-02 date.
- Lowered BNB research-only detector thresholds to `$15K` single / `$50K` burst after local trade distribution showed `$100K` / `$250K` was a dead detector.
- Hardened the liquidation monitor against replay duplication and out-of-order burst duration artifacts; cleaned duplicated replay rows from `data/liquidations.jsonl`.

---

## 2026-08-03 - Live-like BTC/HYPE paper lane

Codex implemented the first live-like paper lane for the actual BTC/HYPE liquidation-regime strategies.

**What changed**
- Added `src/execution/paper_broker.py` with conservative stop/target/trailing simulation.
- Added `scripts/paper_decision_loop.py`, consuming `data/cascades.jsonl` and `data/ws_candle/*.jsonl` without importing the real exchange or `OrderManager`.
- Added `tests/test_paper_decision_loop.py` for same-bar stop ordering, BTC/HYPE route opening, and restart de-duplication.
- Added `LIVE-LIKE PAPER LANE` to `scripts/status.py`.
- Added `docs/2026-08-03-IMPLEMENTATION-live-like-paper-lane.md`.
- Wired `scripts/run_rebuild_cycle.py` to run `scripts/paper_decision_loop.py --once --max-new 500` as the final best-effort step after regime summary.

**First corrected run**
- `scripts/paper_decision_loop.py --once --max-new 500`
- Decisions written: 273.
- Positions opened: 7.
- Positions closed: 7.
- Open now: 0.

---

## 2026-08-03 - BTC/HYPE paper launch plan

Codex added the paper-launch plan for the actual BTC/HYPE liquidation-regime lanes.

**Decision**
- Start planning paper now; paper is the live-sequence calibration lab, not the final proof step.
- BTC B-side failed-reclaim continuation with trailing-resolution exit is the v1 paper candidate.
- HYPE B-side range/liquidation scalp is research-paper only.
- ETH/SOL/DOGE/BNB/xyz:GOLD/xyz:SILVER remain collect-only.

**What changed**
- Added `docs/2026-08-03-PLAN-paper-launch-btc-hype.md`.
- The plan calls for a new `scripts/paper_decision_loop.py` instead of bending the existing HyperPerps heatmap `paper_trade_loop.py`, because the old loop does not paper the exact BTC/HYPE liquidation-regime lanes.

---

## 2026-08-03 - Post-rebuild regime summary (Marvis)

Marvis started logging regime evidence per the regime-map handoff checklist.

**What changed**
- Added `scripts/run_regime_summary.py` (collector, no interpretation): implements the 7-step checklist from `docs/2026-08-03-HANDOFF-regime-map.md` (rebuild metadata, per-symbol regime counts, BTC watch pocket from trailing sweep, ETH rejected lane, HYPE research pocket by band_width, safety gate).
- Added `tests/test_regime_summary.py` (13 focused tests for the pure helpers, all passing).
- Wired the script into `scripts/run_rebuild_cycle.py` as the 11th command in the chain (best-effort, does not block baseline). Cycle now runs: build_cascades, fade_or_follow backtest, 2 lane backtests, 2 focused side-filtered runs, 3 TP/SL sweeps, 1 trailing sweep, 1 regime summary.
- Output appends to `data/regime_log/regime_summary_YYYYMMDD.jsonl` (gitignored via `data/`).

**First run (2026-08-04 01:05 UTC rebuild, commit de13bb7)**
- 679 cascades (BTC 284, ETH 212, HYPE 102, SOL 66, DOGE 15; B 342 / A 337).
- BTC watch pocket: best is failed_reclaim_continuation 240m event_vwap 15bps 1.5R 10bps trail, n=37, PF 1.09, med +0.0896%. Gate: n=False, pf=False, median=True. n and PF need to grow.
- ETH rejected lane: 6 buckets, no crossed gate. Largest n=99 (baseline_fade A) with PF 0.98.
- HYPE research pocket: 3 compressed (PF 0.45, all losses), 5 normal (PF 1.22), 2 wide (PF inf, all wins). Compressed bucket continues to lose.
- Safety gate: 0 violations. SOL/HYPE/DOGE/BNB all execution_allowed=False.

**Bugs caught and fixed during this work**
- BTC watch pocket: `pf_met` was treating inf as 0 (false). Now uses `pf > threshold` directly, which handles inf correctly.
- ETH rejected lane: same pf-vs-inf bug.
- Regime script: HYPE trades use `net_return_pct`, not `return_pct`. Bucketing now reads `band_width_pct` and routes through `band_width_bucket()`.

---

## 2026-08-03 - Deterministic regime map

Codex added the first rule-based regime layer for AI-assisted strategy routing.

**What changed**
- Added `src/strategy/regime.py` with deterministic candle-regime labels, liquidation-response labels, and asset-specific routing decisions.
- Added `tests/test_regime.py` covering BTC B-side continuation watch routing, ETH rejection, HYPE B-side range research routing, and compressed HYPE rejection.
- Added `docs/2026-08-03-HANDOFF-regime-map.md` with the Marvis delegation checklist and promotion guard.

**Current read**
- BTC B-side failed-reclaim continuation remains a v1 watch pocket, not auto-trade.
- ETH remains rejected under the current framing.
- HYPE B-side normal/wide range remains research-only; compressed HYPE remains rejected.
- SOL/DOGE/BNB remain collect-only.

---

## 2026-08-03 - Secret ignore hardening

Codex tightened `.gitignore` for local secrets and credential exports.

**What changed**
- `.env.*` files are now ignored while `.env.example` stays tracked.
- Added ignore patterns for credential JSON, wallet files, private keys, certs, token dumps, MiniMax config, and external data provider local configs.
- Ran a tracked-file secret-shaped scan; hits were placeholders/env-var references, not committed secret values.

---

## 2026-08-03 - External liquidation data source sweep

Codex used MiniMax as a low-cost scout for external Hyperliquid data sources and logged the acceleration plan.

**What changed**
- Added `docs/2026-08-03-RESEARCH-external-liquidation-data-sources.md`.
- Mirrored the research note into the Obsidian vault.
- Added optional read-only external data API placeholders to `.env.example`.

**Current read**
- Moon Dev is the highest-priority data source because it can provide both 30d liquidation windows and BTC/HYPE near-liquidation position snapshots.
- 0xArchive, Hyperliquid official S3/node data, Allium, Tardis, BlockLiquidity, and PurrData are secondary validation/backfill paths.
- The goal is to validate BTC and HYPE with more data, not to promote either lane yet.

---

## 2026-08-03 - BTC trailing stability report

Codex added an analysis-only stability report for the BTC/ETH trailing candidate.

**What changed**
- `scripts/run_trailing_stability_report.py` compares the same trailing candidate across evaluation horizons and candle-coverage horizons.
- The report prints overall rows plus chronological folds so a promising row can be checked for coverage bias and one-period dependence.
- `tests/test_trailing_stability_report.py` covers the report summary math.

**Decision use**
- The current BTC B-side trailing pocket should not be promoted just because a 240m-mature subset looks good.
- Marvis should use this report after each mature rebuild before escalating any BTC/ETH trailing result.

---

## 2026-08-03 - BTC/ETH trailing resolution sweep

Codex added a longer-horizon trailing-stop research lane for BTC/ETH.

**What changed**
- `src/strategy/lane_backtest.py` now supports initial-stop plus trailing-stop exit analysis.
- `scripts/run_trailing_sweep.py` sweeps 30/60/120/240m hold windows, initial stop models, activation R, and trailing bps.
- `tests/test_lane_backtest.py` covers trailing activation, initial-stop exit before activation, and trailing summary rates.
- `docs/2026-08-03-HANDOFF-trailing-resolution.md` records commands and the current decision read.

**Current read**
- BTC B-side failed-reclaim continuation is the first watchlist-quality BTC pocket: 120/240m, event-VWAP stop with 15-25 bps buffer, activation near 2R, 10 bps trail, PF about 1.55-1.60.
- Caveat: the positive pocket is only n=30 because 240m coverage excludes newer live-edge events.
- ETH trailing remains weak; best checked rows stayed negative with PF below 0.70.
- No execution promotion; next step is to see if the BTC B-side trailing pocket survives as more mature data arrives.

---

## 2026-08-03 - Structure-aware TP/SL sweep

Codex extended the TP/SL analysis beyond fixed-bps stops.

**What changed**
- `src/strategy/lane_backtest.py` now supports ATR stops and event-VWAP invalidation stops in the analysis layer.
- ATR stops use prior completed 1m candles only.
- Event-VWAP stops apply an adverse VWAP buffer and skip impossible stop placements.
- `scripts/run_tp_sl_sweep.py` can sweep `fixed_bps`, `atr`, and `event_vwap` stop models together.
- One-off `run_lane_backtest.py --exit-model r_multiple` reports average effective stop bps.

**Current read**
- BTC B-side still fails: best expanded-grid PF about 0.40, and structure-aware exits did not improve it.
- ETH still fails: best expanded-grid PF about 0.41.
- HYPE B-side remains watchlist-only: fixed 30 bps / 1.0R is still the best tiny-sample result around PF 1.44; ATR did not improve it.
- The next research question is entry quality/filtering, not just exit settings.

---

## 2026-08-03 - TP/SL sweep infrastructure

Codex added a data-driven exit-analysis layer for lane entries.

**What changed**
- `src/strategy/lane_backtest.py` now supports R-multiple TP/SL re-scoring with MAE/MFE, stop/target/timeout exit reasons, stop rate, target rate, and average R.
- `scripts/run_lane_backtest.py` now accepts `--exit-model r_multiple`, `--stop-bps`, and `--target-r`.
- `scripts/run_tp_sl_sweep.py` sweeps fixed raw-price stops against R-multiple targets for a lane/symbol/side.
- `tests/test_lane_backtest.py` covers target hits, stop hits, timeout/cost behavior, and conservative same-bar stop-first ordering.
- `docs/2026-08-03-HANDOFF-tp-sl-sweep.md` records commands, assumptions, and the current decision read.

**Current read**
- BTC B-side fails the first fixed-bps TP/SL grid: best PF about 0.40 after costs.
- ETH fails the same grid: best PF about 0.41 after costs.
- HYPE B-side remains watchlist-only: n=10, best tested fixed-bps combo about PF 1.44.
- No execution promotion.

---

## 2026-08-03 - TP/SL research note

Codex added `docs/2026-08-03-RESEARCH-tp-sl-settings.md`.

**Decision**
- BTC/ETH `fade_or_follow` is not strategy-complete because it currently has no real TP/SL model, only a fixed 15-minute exit.
- Alt `range_sweep_liq_scalp` already has a structural TP/SL: target mid-band, stop beyond outer band plus 5 bps.
- Future backtests should report R-multiples, not only raw percent returns.
- Initial BTC/ETH TP/SL grid should test fixed bps, event-VWAP invalidation, ATR stops, and TP ratios from 1.0R to 2.5R.

---

## 2026-08-03 - Mature-row rebuild trigger fix

Codex fixed the rebuild trigger so active liquidation streams do not starve the backtest cycle.

**What changed**
- `src/strategy/rebuild_trigger.py` now counts `mature_new_rows`: rows after the baseline line count whose event timestamps are at least 30 minutes old.
- `scripts/run_rebuild_cycle.py` now prints both total new rows and mature new rows.
- This replaces the overly strict behavior where the latest event in the whole file had to be 30+ minutes old, which can fail forever during active markets.

**Verification**
- Trigger check now fires with 1,226 mature new rows even though the newest liquidation is only a few minutes old.
- Rebuild cycle succeeded: 2,593 raw liquidation rows -> 621 cascades.

**Updated strategy read**
- BTC B-side focused report deteriorated on the larger sample: `reclaim_fade` n=85, PF 0.72; `baseline_fade` n=122, PF 0.81.
- HYPE B-side reached n=10 but remains below promotion threshold: PF 1.34, median +0.0207%.
- No lane should be promoted to execution.

---

## 2026-08-03 - Lane diagnostics added

Codex added `--diagnostics` to `scripts/run_lane_backtest.py` and diagnostic helpers in `src/strategy/lane_backtest.py`.

**What it shows**
- Per-symbol, side, direction, exit-reason, and band-width bucket summaries.
- Largest-win share of gross profit, so outlier-carried edges are visible.

**Current read**
- BTC/ETH blended edge is flat: all lane trades together PF ~1.00.
- BTC B-side fade is the only strong BTC/ETH pocket so far: n=120, PF 1.64.
- BTC A-side fade is bad: n=118, PF 0.66.
- ETH is flat on both sides.
- HYPE B-side alt range scalp is the only alt pocket worth watching: n=7, PF 1.92, but still too small.
- HYPE compressed-band trades are bad in this sample: n=6, PF 0.15.

**Decision**
- No execution promotion.
- Marvis should run both lane backtests with `--diagnostics` after each rebuild and send changes to Codex/Slim.

**Follow-up**
- Added `--side A|B` to `scripts/run_lane_backtest.py` so focused reports can isolate BTC B-side and HYPE B-side without overwriting blended output files.
- Current BTC B-side focused report: `reclaim_fade` n=42, PF 1.81, median +0.0248%; `baseline_fade` n=60, PF 1.65, median +0.0096%.
- The next serious research candidate is BTC B-side-only reclaim fade, with BTC B-side baseline fade as the control.

---

## 2026-08-03 - Lane backtester scaffold

Codex added a repo-native lane backtester so BTC/ETH v1 research and alt research can be tested separately without moving the project into TradingView or a third-party bot framework.

**What changed**
- Added `src/strategy/lane_backtest.py`.
- Added `scripts/run_lane_backtest.py`.
- Added `tests/test_lane_backtest.py`.
- Added `docs/2026-08-03-HANDOFF-lane-backtester.md` for Claude Code extension work.

**Lane behavior**
- `btc_eth_fade_or_follow` reuses the existing BTC/ETH backtest path.
- `alt_range_liq_scalp` is research-only for SOL/HYPE/DOGE/BNB.
- The alt lane tests liquidation bursts at Bollinger extremes, requires a close back inside the band, fades toward mid-band, stops beyond the outer band, and subtracts round-trip costs.

**Verification**
- Focused tests passed: 46 tests.
- Current alt data is not decision-grade: 16 alt cascades produced only 2 confirmed range-lane trades.

---

## 2026-08-02 (late) - Clustering + timing guard run on cleaned data

Codex took the hard-parts side of the role split and reran the cascade/backtest path on the cleaned live data.

**What changed**
- `cascade_cluster.py` keeps liquidation clusters isolated by `(symbol, side)` so interleaved symbols cannot split a valid BTC/ETH cascade.
- `event_features.py` keeps `vwap_check` equal to event VWAP and moves average fill size into `avg_fill_notional`.
- `build_cascades.py` applies bounded nearest-snapshot joins and rejects L2/context snapshots outside `--max-snapshot-lag`.
- `run_fade_or_follow_backtest.py` reads every `data/ws_candle/{symbol}_*.jsonl` file instead of one hard-coded date, deduping each 1m candle to the final websocket update.
- `fade_or_follow_backtest.py` enforces `--max-entry-lag` so old cascades cannot enter against candles that start too late.

**Verification**
- Syntax check passed for the touched strategy/backtest scripts.
- Focused unittest suite passed: 40 tests.
- Latest rebuild: 680 raw liquidation events -> 183 cascades.
- Latest guarded 15m backtest loaded all candle coverage: BTC/ETH/SOL/HYPE 950 1m candles each, DOGE 175.

**Read on the data**
- BTC baseline fade remains mildly positive: n=69, WR 52.2%, avg +0.0236%, PF 1.66.
- ETH reclaim fade remains better than ETH baseline, but the edge cooled after loading the newer candle files: n=29, WR 48.3%, avg +0.0275%, PF 1.51.
- BTC/ETH failed-reclaim continuation is not supported by this sample.
- SOL/HYPE/DOGE remain research-only; samples are too small for execution decisions.

---

## 2026-08-02 (late) — Two-track strategy split, two new research docs

User finished the focused BTC/ETH strategy sweep and committed the result to `docs/2026-08-02-RESEARCH-btc-eth-hyperliquid-strategy-sweep.md`. A second hybrid-strategies doc (`docs/2026-08-02-RESEARCH-hybrid-liquidation-strategies.md`) covers the SOL/HYPE / shared-primitives framing.

**What changed in AGENTS.md**
- TL;DR reframed: "single strategy class" → "liquidation-aware derivatives-flow, asset-routed into two tracks"
- Strategy section now describes both tracks: BTC/ETH `liquidation_fade_or_follow` (v1, build now) and SOL/HYPE `range_sweep_liquidation_scalp` (Phase 2, gated on BTC/ETH validation)
- §2 "no second strategy in parallel" clarified: the two tracks are NOT a violation (same trigger + primitives, asset-routed response classifier). A genuinely new strategy (trend-following, funding arb) is still blocked until v1 validates.
- Quick reference + changelog bumped to reflect two-track design

**What did NOT change**
- Out of scope list (gold/silver, ML pre-100-trades, leverage > 10x, etc.) — all still blocked
- Risk framework (AGENTS §5) — unchanged
- Daemon set (5 daemons, 4 symbols) — unchanged
- Build phase order — still 4-week v1, BTC/ETH first

**Open work the strategy sweep identified** (unblocks needed before spec lands)
- 1m candle subscription (currently 1h only) — needed for VWAP / band features at scalp time horizons
- Sub-5min OI delta (poll_asset_ctx is 5min cadence, just barely adequate)
- Book state history (we have l2book snapshots but not time-series)
- Event-level feature store at detection time (per spec: "event VWAP, pre/post price, OI before/after, funding, spread, top-book imbalance")

Mirrors to vault: `research/2026-08-02-BTC-ETH-HYPERLIQUID-STRATEGY-SWEEP.md` and `research/2026-08-02-HYBRID-LIQUIDATION-STRATEGIES.md`.

---

## 2026-08-02 — Scope expanded to BTC/ETH/SOL/HYPE

User asked to add SOL and HYPE to the data capture. Four-symbol monitoring is now live on all 5 daemons.

**Per-source breakdown**
- `poll_hyperperps.py` — BTC, ETH, SOL (HYPE returns `sample_size=0` from the HyperPerps heatmap API, so no heatmap-based metrics for HYPE)
- `paper_trade_loop.py` — same 3 symbols as the HyperPerps poller
- `collect_ws_data.py` — all 4 symbols, all 5 channels (trades, l2Book, candle, activeAssetCtx, bbo)
- `liquidation_monitor.py` — globs `data/trades/*.jsonl`, so any symbol with WS trades gets monitored for free
- `poll_asset_ctx.py` — all 4 symbols (mark/OI/funding/predicted)
- `fetch_historical.py` — all 4 symbols, 7d backfill done for SOL ($73.08 last) and HYPE ($51.92 last)
- `OrderManager` — extended hardcoded fallback `ticks` (`$0.001` for HYPE) and `decimals` (`2` for HYPE, matching `meta()` `szDecimals`). Prefer-path through `meta()` is unchanged.

**Live data first read (after ~2.5 min)**
- SOL: 220 trade records (very active)
- HYPE: 268 trade records (most active by count, even more than ETH)
- BTC/ETH continue flowing

**Housekeeping**
- Killed a duplicate `liquidation_monitor` process that had been running in parallel (different Python install, double-counting events). Restarted cleanly.
- AGENTS.md scope + leverage-cap table + GitHub description updated.
- 104/104 tests still passing.

**Out of scope (still)**
- PAXG, XAU/XAG, MT5/MQL5, ML on synthetic backtests, second strategy in parallel, leverage > 10x, PAXG standalone — all unchanged. HYPE is added as a perp, not as a "second strategy."

---

## 2026-08-01 (late session) — status.py progress bar + SDK reinstall

Added a `BACKTEST READINESS` section to `scripts/status.py` so the user can see how close we are to the auto-backtest guard (100 events AND 24h old) at a glance.

**What you see now**
- Events: `78/100 [#######-----] 78%  (last 1h: +14)`
- Age:    `3.48h / 24h [##---------] 14.5%  (oldest: 02:10:37 UTC)`
- Next gate: `~1h to 100 events (at 14/h) | 20h 31m until unlock (Mon 02:10 UTC)`

**Files**
- `scripts/status.py` — added `show_backtest_readiness()` with two ASCII bars, throughput from last 1h, and a single "next gate" line
- `scripts/test_pagination.py` → `scripts/probe_pagination.py` (pytest was auto-collecting it; it was a one-off probe, not a real test)

**Housekeeping**
- The `hyperliquid-python-sdk` and `eth-account` packages were missing from the env (likely a venv reset). Reinstalled from a fresh local clone of the GitHub repo. Tests now `104/104` passing again.
- Committing the AGENTS.md / changelog / research-repost deltas that Codex left pending from earlier handoffs.

---

## 2026-08-01 — Order Manager Hard-Parts Pass (Codex)

Codex reviewed `src/execution/order_manager.py` against the order manager review brief and patched the parts that can create real execution risk. Repo venv and pytest are healthy (101/101 pass). Details in `docs/2026-08-01-HANDOFF-order-manager-hard-parts.md` and the matching vault research note.

**Safety changes**
- `SignalDirection.NO_TRADE` is now rejected explicitly instead of being treated as a short.
- ATR fallback trading removed: empty/short candle history now refuses to size a position rather than synthesizing a stop distance.
- Default `bulk_orders` grouping switched from `positionTpsl` to `normalTpsl` (parent entry + TP/SL children). `positionTpsl` is back-burnered pending testnet proof.
- Orphan-entry handling added: if the entry lands but a child TP/SL errors, the manager attempts `bulk_cancel` on the entry, sets `status="orphan_error"`, and flags `needs_reconciliation=True`.
- `OrderResult` extended with `status`, `size_coin`, `needs_reconciliation`, `cancel_attempted`, per-leg statuses, and the cancel response.
- Size rounding prefers Hyperliquid `meta()` `szDecimals`; falls back to hardcoded BTC/ETH/SOL only if the call fails.

**Tests** (101/101 pass on the repo venv, 3 new + 1 renamed)
- `test_no_trade_signal_rejected_not_short`
- `test_refuses_to_trade_without_atr_history`
- `test_orphan_entry_attempts_cancel_when_child_order_fails`
- `test_bulk_orders_called_with_positionTpsl_grouping` → renamed to `..._normalTpsl_grouping`

**Files**
- `src/execution/order_manager.py`
- `tests/test_order_manager.py`
- `docs/2026-08-01-HANDOFF-order-manager-hard-parts.md` (durable handoff, mirrored to vault)

**Out of scope (per handoff "Mavis must not" list)**
- No mainnet orders, no risk-limit changes, no re-enable of ATR fallback, no revert to `positionTpsl` without testnet proof, no ML/DCA/martingale/new assets/venues.

---

## 2026-08-01 — Testnet vs Mainnet Reality Check (CRITICAL FINDING)

**The testnet backtest was a fiction. Mainnet data is fundamentally different.**

After fetching 90 days of mainnet data, the funding rate distribution is **100-200x smaller and 100x smoother** than testnet:
- Testnet BTC funding range: -0.296% to +0.704% per hour
- Mainnet BTC funding range: -0.0027% to +0.0019% per hour
- The 0.10% / -0.05% testnet thresholds NEVER fire on mainnet

**Mainnet backtest (90d, scale-appropriate thresholds):**
- Only 32% of 25 sweep configs profitable
- CoV 1.55 (UNSTABLE)
- Median return -4.28% (slightly negative)
- Best config: high=0.0020%, low=-0.0025%, +1.85% return on **6 signals** (not statistically meaningful)
- Going to lower thresholds (more signals) makes it WORSE — 160 signals = -13%, 2700 signals = -87%

**Directional check (full 2160 events, 100% matched):**
- 84% of funding events are positive (longs pay shorts) — this is just steady-state premium, not extreme positioning
- BTC drifted from $80K to $63K in 90d while 84% of funding was positive
- The "extreme high" funding event (>1.5e-5, 1 event): price went UP 154 bps over 24h
- The "extreme low" funding events (BTC < -1.5e-5, 28 events): price went DOWN 24 bps over 24h

**Implication: the simple "funding extreme = cascade signal" hypothesis is structurally wrong on this market.** Funding is too small, too constant, and follows trend rather than predicting reversal. The testnet edge was an artifact of synthetic chaotic funding.

**Where the real edge probably lives:** actual liquidation EVENTS, not inferred from funding. Hyperliquid publishes liquidation data on-chain. A signal based on real liquidations (price impact, volume signature, OI drop) is the next research direction.

**Code:**
- `scripts/fetch_historical.py` — `HYPERLIQUID_ENV` env var (testnet | mainnet); filenames now include env suffix
- `scripts/run_backtest.py` — `_find_data()` prefers mainnet > testnet, longest lookback first
- `scripts/mainnet_sweep.py` — mainnet-scale parameter sweep (NEW)
- `scripts/_funding_dist.py` and `scripts/_debug_loader.py` — debugging aids (can be removed)
- `scripts/_funding_vs_price_v2.py` — directional check (proves strategy is structurally wrong)
- Data: 90d BTC+ETH mainnet, 90d BTC+ETH testnet, 30d BTC+ETH testnet all in `data/`

---

## 2026-08-01 — 90-day Backtest Pass

- `scripts/fetch_historical.py` — added `import pandas as pd` (NameError fix); pagination walks 20-day chunks to bypass 500-event API cap
- `scripts/run_backtest.py` — `load_data()` now picks the longest lookback file available (90d > 30d > 7d)
- Fetched 90 days: 2161 candles + 2160 funding events per symbol (BTC + ETH)
- Backtest on 90d: 387 trades, 59.4% WR, PF 5.89, 100% of sweep configs profitable, walk-forward CONSISTENT (3/3 folds green)
- **Caveat: this is TESTNET data, not mainnet.** Funding dynamics may differ.
- See `vault/changelog.md` 2026-08-01 entry for full results

---

## 2026-08-01 — AI / MCP / Codex Sweep

- Added `docs/2026-08-01-RESEARCH-REPOST-ai-mcp-codex.md`.
- Vault note `research/2026-08-01-AI-MCP-CODEX-SWEEP-btc-eth-liquidation.md` enumerates 30+ public Hyperliquid AI/MCP repos with verified primary-repo descriptions.
- Confirmed Claude Code Hyperliquid MCP installs via `claude mcp add ... -- npx -y <package>`; Codex uses the same stdio MCP and `.agents/skills/<name>/SKILL.md` path; `npx skills add <owner/repo>` is the vendor-agnostic installer.
- No first-party Codex Hyperliquid plugin was found.
- New adoption deltas: agent (API) wallet for the bot; bracket on every entry; 128-bit hex client order IDs; OCO grouping rules; WebSocket 4-channel pattern with reconnect-with-gap; liquidation dataset bootstrap via `0xArchiveIO/0xarchive-mcp`; decision recorder to `data/decisions_*.jsonl`; plugin-based protections; ATR/pre-event-level TP/SL.
- No LLM in the trade loop in v1. AI is for research and skill wiring only.
- Scope unchanged: BTC/ETH, Hyperliquid only, 10x leverage cap, 1% risk/trade, strict circuit breakers.

- Added `docs/2026-08-01-RESEARCH-REPOST-btc-eth-liquidation.md` for AI-editor context.
- Reviewed public GitHub projects covering Hyperliquid liquidation alerts, wallet tracking, and trading infrastructure.
- Attempted Reddit, X, and YouTube searches; access/rendering limitations are recorded rather than presenting unverified claims as evidence.
- Decision unchanged: BTC/ETH only, Hyperliquid only, one cascade strategy, strict risk controls, and no ML before 100+ own live trades.



### Overview
Major pivot: dropped the gold/silver scope, leaned into the cascade edge on crypto. Also laid down the full project skeleton (code, tests, risk module) in the same day.

### Decisions made
- **PIVOT:** G/S Hyperliquid → **BTC/ETH liquidation cascade** on Hyperliquid. Reason: Week-0 market recon showed PAXG is the only metal perp on HL, too thin for a $500+ strategy (0.13% of BTC's daily volume, max 10x leverage). The cascade edge (the most defensible Hyperliquid moat) is a crypto phenomenon. See `vault/notes/2026-08-01-PIVOT-btc-eth-cascade.md`.
- **Single project, $1,000 bankroll.** Concentrate capital on the highest-EV play.
- **Conservative ramp:** $50 canary → $200 → $500 → $1,000 over 6-8 weeks.
- **Honest targets:** 12-15% monthly, 55-65% WR, 2.0+ PF.

### Code shipped
- `AGENTS.md` — scoped MD file for AI editors (Claude, Cursor, OpenCode, Codex, Aider, etc.)
- `README.md`, `.gitignore`, `.env.example`, `requirements.txt`
- `config/settings.example.yaml`
- `src/risk.py` — risk module with full test coverage
- `tests/test_risk.py` — 16 tests covering all 8 risk verdicts
- `src/{exchange,strategy,execution,journal}/__init__.py` — module skeletons

### Open
- [ ] First commit + push to GitHub
- [ ] Update GitHub repo description from "Testing Gold and Silver Strategies" to "BTC/ETH cascade bot on Hyperliquid"
- [ ] Week 1: testnet auth proof
- [ ] Decide: rename vault folder from `gold-silver-hyperliquid` to something crypto-themed?

---

## 2026-08-01 — Project Init (earlier same day)

- Project folder created on GitHub (`KJohnson-700/HyphyLiquid`) and local workspace
- Market recon script shipped (`scripts/market_recon.py`)
- Recon results saved to `scripts/market_recon_raw.json`
- Obsidian vault structure initialized

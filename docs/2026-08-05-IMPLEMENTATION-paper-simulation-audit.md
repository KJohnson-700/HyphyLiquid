---
date: 2026-08-05
type: implementation
project: hyphyliquid
status: active
---

# Paper Simulation Audit

## Why

The live-like paper lane is now the calibration layer before any live-capital decision. That means the paper ledger has to answer basic trust questions without manual JSONL spelunking:

- Which candidates were accepted or rejected?
- Which paper positions opened?
- How did each position exit?
- What was the realized net return, PnL, and R multiple?
- Did any paper trade violate the current gate/scope/risk assumptions?

## What Was Added

- `scripts/run_paper_audit.py`
  - Reads `data/paper_decisions_*.jsonl` and `data/paper_positions_*.jsonl`.
  - Writes `data/paper_audit_latest.json`.
  - Writes `data/paper_audit_latest.md`.
  - Reports decision counts, reject reasons, closed-fill metrics, symbol/lane breakdowns, recent opens, active anomalies, and legacy warnings.

- `scripts/run_rebuild_cycle.py`
  - Runs the paper audit as the final best-effort step after `scripts/paper_decision_loop.py --once --max-new 500`.
  - Audit failure warns but does not block the rebuild baseline.

- `src/execution/paper_broker.py`
  - Fixed the missing-entry-candle mark path so recovered positions stay open/unmarked instead of failing if the candle list is shorter than the stored `entry_idx`.

## First Audit Read

Generated from the current paper ledger after the 2026-08-05 08:06 UTC rebuild:

- Decisions: 948
- Opened positions: 21
- Closed fills: 21
- Open now: 0
- Current anomalies: 0
- Legacy warnings: 20

The 20 legacy warnings are BTC paper opens from before the current `ask_heavy` top-book imbalance gate existed. They should not be mixed with the current filtered BTC paper rule when judging forward paper performance.

Current paper performance across all historical paper opens remains weak:

- Net PnL: -$161.55
- PF: 0.3178
- Win rate: 28.57%
- Avg/median net return: -0.1569% / -0.2955%
- Exit reasons: 15 initial stops, 4 trailing stops, 2 timeouts without activation

## Decision

Do not use the full 21-trade paper aggregate as the current BTC gate score. It contains pre-gate legacy opens. Judge the current setup only on post-gate paper opens where:

`BTC + side=B + failed_reclaim_continuation + top_book_imbalance_bucket=ask_heavy`

The audit exists to keep that distinction visible after each rebuild.

## Post-Rebuild Context

The rebuild succeeded and refreshed the audit as the final best-effort step.

- BTC side=B total n=506 crossed the sample-count ping threshold, but the actual reclaim/follow reads remain weak.
- BTC B-side reclaim_fade: n=176, PF=0.70.
- BTC B-side trailing best: n=73, PF=1.03, median +0.0319%, below the n>=100 and PF>1.5 promotion gate.
- HYPE side=B reached n=20, but the full side=B aggregate remains PF=0.92 with median -0.0519%.

Interpretation: keep collecting and papering; do not promote live.

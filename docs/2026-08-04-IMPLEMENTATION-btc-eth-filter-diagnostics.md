---
date: 2026-08-04
type: implementation
project: hyphyliquid
status: active
---

# BTC/ETH Filter Diagnostics

## Why

The larger BTC/ETH sample weakened the simple fade/reclaim hypothesis:

- BTC B-side reclaim_fade: n=147, PF 0.69 on the latest focused run.
- BTC B-side failed_reclaim_continuation: n=62, PF 1.35, median +0.0055%.
- BTC B-side trailing best: n=60, PF 1.13, median +0.0474%.
- ETH remains rejected across tested buckets.

That means the next useful work is not another generic fade/reclaim sweep. The next useful work is deterministic filtering around event context: BBO spread, top-of-book imbalance, funding, OI level, liquidation size, fill count, and snapshot freshness.

## What Was Added

- `src/strategy/filter_diagnostics.py`
  - Joins BTC/ETH lane trades back to enriched cascades.
  - Buckets spread, imbalance, funding, notional, fills, L2 staleness, context staleness, and per-symbol OI level.
  - Produces grouped PF/median/outlier diagnostics without touching execution.

- `scripts/run_filter_diagnostics.py`
  - Reads `data/cascades.jsonl` and `data/lane_backtest_btc_eth_fade_or_follow_trades.jsonl`.
  - Writes `data/btc_eth_filter_diagnostics.json`.
  - Prints the top ranked filter buckets.

- `scripts/run_rebuild_cycle.py`
  - Runs the filter diagnostic as a best-effort step after the BTC B-side trailing sweep.

## Latest Read

The strongest bucket in the latest run:

- `failed_reclaim_continuation | BTC | side=B | top_book_imbalance=ask_heavy`
- n=21
- win rate 71.4%
- avg +0.0760%
- median +0.0448%
- PF 3.23
- top_win_share 24.2%

This is promising but still below the promotion sample threshold. Treat it as the next paper-gate candidate, not as proof.

Other useful buckets:

- `BTC side=A baseline/reclaim with high OI` also screened well, but that is less aligned with the current BTC B-side watch lane.
- `ETH side=B ask_heavy baseline_fade` screened PF 1.73 at n=71, but ETH's full lane remains weak, so do not promote ETH yet.

## Next Action

Codex should implement a paper-only gate candidate for:

`BTC side=B + failed_reclaim_continuation + top_book_imbalance_bucket == ask_heavy`

Implemented in the live-like paper loop after the first diagnostic read. BTC paper positions now require the filtered `ask_heavy` top-book bucket before opening. This remains paper-only and does not change the `OrderManager` execution allowlist.

## Paper Accuracy Fix

Codex tightened the paper simulator on 2026-08-04 after reviewing live drift risks:

- Fixed `scripts/paper_decision_loop.py` so `max_new` no longer means "only inspect the last N cascades." The loop now selects the oldest unprocessed BTC/HYPE candidates first. This matters on heavy market days where more than `max_new` cascades can arrive between runs.
- Updated `src/execution/paper_broker.py` so open marks report unrealized P&L using the latest completed candle. Before this, open positions showed `0.0%` until close, which made status less accurate.
- Added regression tests for both behaviors.

After the fix, one catch-up pass wrote 137 decisions and opened/closed 1 filtered BTC paper position. Treat paper counts before this fix as possibly undercounted during high-volume windows.

Recommended guardrails before live:

- paper-only first
- keep v1 allowlist unchanged: BTC/ETH only
- no HYPE/SOL/DOGE/BNB/metal execution
- require at least n=50 in this bucket before considering promotion
- require PF > 1.5 and median > 0 after costs
- compare with trailing exit, not only fixed 15m exit

Marvis can keep running the rebuild cycle and report this bucket's n/PF/median after each trigger.

# 2026-08-03 - Live-Like BTC/HYPE Paper Lane

## Thesis

Paper trading should start as the live-sequence calibration lab. It should use
the same liquidation/candle inputs and deterministic route logic as the live
bot, while replacing only the exchange order/fill boundary with a conservative
paper broker.

## What was built

- Added `src/execution/paper_broker.py`.
- Added `scripts/paper_decision_loop.py`.
- Added `tests/test_paper_decision_loop.py`.
- Added a `LIVE-LIKE PAPER LANE` section to `scripts/status.py`.

## Current lane behavior

### BTC

- Scope: `v1_paper`.
- Symbol/side: BTC `side=B` only.
- Route: `btc_eth_trailing_resolution`.
- Entry: failed-reclaim continuation after a 3-bar wait.
- Stop: event VWAP minus 15 bps.
- Activation: 2R.
- Trail: 10 bps.
- Max hold: 240 minutes.
- Sizing: targets about $10 risk from the actual stop distance, capped at
  $10,000 notional.

### HYPE

- Scope: `research_paper`; not executable.
- Symbol/side: HYPE `side=B` only.
- Route: `alt_range_liq_scalp`.
- Entry: normal/wide range bucket plus upper-band sweep/rejection confirmation.
- Stop: outer band plus 5 bps buffer.
- Target: mid-band.
- Max hold: 15 minutes.
- Sizing: targets about $10 risk from the actual stop distance, capped at
  $10,000 notional.

## Live-like controls

- The loop consumes local live files: `data/cascades.jsonl` and
  `data/ws_candle/*.jsonl`.
- It does not import or call the real `OrderManager` or exchange SDK.
- The paper broker uses conservative same-bar ordering: stop wins before
  target/trailing.
- Stop exits include an extra 2 bps slippage haircut.
- Round-trip fees/spread/slippage cost defaults to 8 bps.
- Paper IDs are deterministic across restarts.
- Open positions are recovered from the append-only ledger and re-marked on
  later passes.
- Backlog replay is chronological: positions are marked up to each cascade's
  timestamp before new entries are considered.
- The max-open-position guard is enforced during paper replay.

## First corrected run

Command:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe scripts\paper_decision_loop.py --once --max-new 500
```

Result at 2026-08-04 05:48 UTC:

- Decisions written: 273.
- Positions opened: 7.
- Positions closed: 7.
- Open now: 0.

Status now surfaces these counts under `LIVE-LIKE PAPER LANE`.

## Marvis handoff

Marvis can safely do the low-level operator work:

- Run `scripts/paper_decision_loop.py --once --max-new 500` after each rebuild.
- If stable for a few passes, start it as a daemon at a 60-second interval.
- Watch `scripts/status.py` for:
  - decisions increasing,
  - opened/closed counts,
  - open-now count,
  - last mark net return,
  - any stale runtime logs.
- Do not change BTC/HYPE route settings without Codex review.
- Do not promote HYPE beyond `research_paper`.

## Next engineering step

Wire `scripts/run_rebuild_cycle.py` to call the paper decision loop after the
cascade rebuild, then daemonize the paper loop only after one more clean status
check.

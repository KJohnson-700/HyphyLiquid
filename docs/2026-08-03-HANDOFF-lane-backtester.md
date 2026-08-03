---
project: HyphyLiquid
topic: lane-backtester
date: 2026-08-03
status: handoff
owner: Codex
delegate: Marvis ops; Claude Code only if Codex asks for deeper engineering
---

# Lane Backtester Handoff

## What Codex Built

Codex completed the first non-heavy slice: a repo-native lane backtester instead
of migrating to TradingView, Freqtrade, Backtrader, or vectorbt.

Changed files:

- `src/strategy/lane_backtest.py`
- `scripts/run_lane_backtest.py`
- `tests/test_lane_backtest.py`

The runner keeps strategy lanes separate:

- `btc_eth_fade_or_follow` uses the existing BTC/ETH backtest path.
- `alt_range_liq_scalp` is research-only for `SOL`, `HYPE`, `DOGE`, `BNB`.

## Alt Lane Definition

The alt lane is an iron-condor-inspired range scalp, not an options strategy.

Entry candidate:

1. Symbol is one of `SOL`, `HYPE`, `DOGE`, `BNB`.
2. Canonical liquidation cascade exists for that symbol.
3. First eligible 1m candle after the cascade is within `--max-entry-lag`.
4. Bollinger bands are calculated from prior 1m candles only.
5. B-side cascade: candle wicks above upper band and closes back inside.
6. A-side cascade: candle wicks below lower band and closes back inside.
7. Direction fades the cascade.

Exit:

- Target: mid-band.
- Stop: beyond outer band plus `--stop-buffer-bps`.
- Timeout: `--max-hold` minutes.
- Conservative same-bar ordering: stop beats target.
- Net return subtracts `--round-trip-cost-bps`.

## Commands

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane alt_range_liq_scalp
```

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane btc_eth_fade_or_follow
```

Outputs:

- `data/lane_backtest_alt_range_liq_scalp_trades.jsonl`
- `data/lane_backtest_btc_eth_fade_or_follow_trades.jsonl`

## Verification

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe -m unittest tests.test_lane_backtest tests.test_fade_or_follow_backtest tests.test_cascade_cluster tests.test_event_features
```

Result: 46 tests passing.

## First Data Read

Current cleaned data:

- Alt lane: 16 alt cascades, 2 confirmed range-scalp trades.
- DOGE: n=1, net -0.1595%.
- HYPE: n=1, net +0.2040%.
- This is not decision-grade.

BTC/ETH lane:

- BTC baseline fade: n=73, WR 52.0%, avg +0.0220%, PF 1.62.
- ETH baseline fade: n=44, WR 45.5%, avg +0.0085%, PF 1.14.
- BTC failed-reclaim continuation: n=25, avg -0.0366%, PF 0.44.
- ETH failed-reclaim continuation: n=13, avg -0.0342%, PF 0.63.
- BTC/ETH continuation remains unsupported on this sample.

## Marvis Next Steps

Marvis should operate and report on the lane backtester. Marvis should not change
strategy logic unless Codex explicitly asks.

Run after each successful cascade rebuild:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane alt_range_liq_scalp
```

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane btc_eth_fade_or_follow
```

Also run the focused watchlist reports:

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane btc_eth_fade_or_follow --symbol BTC --side B --diagnostics
```

```powershell
C:\Users\AbuBa\Desktop\HyphyLiquid\.tools\notebooklm-cli\Scripts\python.exe C:\Users\AbuBa\Desktop\HyphyLiquid\scripts\run_lane_backtest.py --lane alt_range_liq_scalp --symbol HYPE --side B --diagnostics
```

Report only:

- total cascades by lane,
- candle counts loaded by symbol,
- trade count by lane/symbol,
- win rate, average net return, median net return, profit factor,
- diagnostics output from `--diagnostics`,
- any "SAMPLE TOO SMALL" warnings,
- whether the output files were written.

Do not interpret tiny alt samples as tradable edge. Send the output to Codex/Slim
for interpretation when:

- any alt reaches 10+ confirmed lane trades,
- BTC/ETH results materially change,
- a lane produces zero trades after a large new cascade batch,
- or the runner errors.

## Claude Code Extension Work

Claude Code is reserved for deeper engineering extensions if Codex/Slim requests
it. Claude may extend the backtester mechanically, but must not touch live
execution.

Allowed:

1. Add BBO spread/slippage cost estimation from `data/ws_bbo`.
2. Add L2 depth-based slippage from nearest `data/ws_l2book`.
3. Add optional band compression sweeps:
   - `--max-band-width-pct 0.5`
   - `--max-band-width-pct 1.0`
   - `--max-band-width-pct 1.5`
4. Add per-symbol parameter grid output for alt research only.
5. Add CSV summary export alongside JSONL trades.
6. Add skipped-candidate counters:
   - no candles
   - stale entry
   - insufficient band history
   - no band extreme
   - too wide band
   - no exit

Blocked:

- No live/mainnet orders.
- No OrderManager changes.
- No risk-limit changes.
- No ML.
- No new venues.
- No treating alt results as executable until sample size is decision-grade.

## Decision Rule

Do not consider the alt lane validated until each traded alt has at least:

- 30+ confirmed lane trades,
- profit factor above 1.5 after costs,
- no single outlier responsible for most of the profit,
- stable behavior across at least two band-width settings,
- and manual chart review of the worst 5 losses.

## 2026-08-03 Diagnostic Read

Codex added `--diagnostics` to `scripts/run_lane_backtest.py`. Marvis should use
it on every lane run.

Current BTC/ETH read:

- Blended BTC/ETH is flat: all variants together PF ~1.00.
- BTC B-side fades are the only strong pocket so far: n=120, PF 1.64, median +0.0112%.
- BTC B-side `reclaim_fade` is currently the cleanest sub-variant: n=42, PF 1.81, median +0.0248%.
- BTC B-side `baseline_fade` is also positive: n=60, PF 1.65, median +0.0096%.
- BTC A-side fades are bad: n=118, PF 0.66.
- ETH is flat on both sides: side A PF 0.94, side B PF 0.97.
- Failed-reclaim continuation remains unsupported.

Current alt read:

- HYPE is the only alt with enough activity to watch: n=11, PF 1.52, median -0.0524%.
- HYPE B-side is better than HYPE A-side: side B n=7, PF 1.92, median +0.0937%.
- Compressed bands are bad in this sample: HYPE compressed n=6, PF 0.15.
- Normal/wide bands look better but are tiny and partly outlier-driven.

Codex call:

- Do not promote any lane to execution.
- Next research filter candidate is BTC B-side-only reclaim fade, with BTC B-side baseline fade as control.
- HYPE B-side normal/wide band scalp stays on watch only.
- Continue collecting before coding those as execution rules.

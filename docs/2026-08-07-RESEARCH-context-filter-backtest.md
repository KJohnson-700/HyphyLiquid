# Context Filter Backtest

Date: 2026-08-07
Scope: research only
Symbols swept: BTC, ETH, SOL, HYPE, DOGE, BNB
Source script: `scripts/run_context_filter_backtest.py`
Outputs: `data/context_filter_results.json`, `data/context_filter_summary.md`

## Purpose

The prior simple liquidation filters kept decaying. This sweep tests genuine new context features that use data we already collect:

- Funding Z-score at cascade time.
- OI delta x price delta regime.
- Time since previous same-symbol cascade.

No execution, risk, order manager, or paper/live routing changed.

## Result

The all-symbol sweep processed 2,309 feature rows and produced 6,664 bucket verdicts. Nine buckets passed the standard gate.

Top passed buckets:

- HYPE side=A fade 60m, cooldown 15-60m: n=60, PF 1.52, median +0.0452%.
- HYPE side=B follow 30m, price up + OI up: n=56, PF 1.72, median +0.1411%.
- HYPE side=B follow 60m, price up + OI up: n=56, PF 1.58, median +0.0414%.
- HYPE side=A fade 60m, cooldown 5-15m: n=54, PF 1.73, median +0.1181%.
- SOL side=B follow 60m, cooldown <5m: n=45, PF 1.50, median +0.0038%.
- ETH side=A follow 60m, funding Z-score positive elevated: n=38, PF 1.68, median +0.0780%.

BTC had no passing bucket.

## Interpretation

The useful signal is context, not a tighter threshold on the old rule.

- ETH: the only v1-symbol candidate is side=A follow over 60m when funding is positively elevated. This is logically coherent: crowded-long funding plus a long-liquidation cascade can continue lower over a longer resolution window.
- HYPE: OI/price and cooldown context materially improved the research lane. This reinforces HYPE as the best alt research candidate, but it remains research-only.
- SOL: one cooldown bucket technically passed, but the median is near zero. Treat as watch-only, not a priority.
- BTC: still not rescued by Tier 1 context. BTC needs Tier 2 depth features next: OBI-5/10/20, OFI, and book resilience from `data/ws_l2book`.

## Decision

Keep BTC/ETH execution scope unchanged. Do not promote any new lane yet.

Next engineering priority:

1. Paper/probe ETH side=A follow 60m under funding-positive-elevated context.
2. Use HYPE context buckets for research-only alt experiments.
3. Build the Tier 2 L2 depth feature pipeline for BTC, because Tier 1 did not rescue BTC.


---
date: 2026-08-05
type: plan
project: hyphyliquid
status: active
---

# AI Regime and Tape Co-Pilot

## Decision

AI is useful for HyphyLiquid, but not as an unconstrained order executor.

The right architecture is an AI co-pilot that reads messy context and helps choose among pre-approved playbooks:

- regime shifts
- tape behavior
- indicator disagreement
- marginal BTC/ETH setups
- daily news/macro/exchange context
- post-trade interpretation

The deterministic bot still owns:

- signal construction
- order placement
- sizing
- stops
- take profit/trailing rules
- `risk.py`
- `OrderManager`

## Contract Added

- `src/strategy/ai_advisory.py`
  - Defines bounded advisory packets.
  - Validates AI responses fail-closed.
  - Allows only `stand_down`, `maintain`, `paper_only`, or `watch_playbook`.
  - Requires evidence keys: `regime`, `tape`, and `risk`.
  - Refuses direct execution requests.
  - Refuses promotion of research symbols into execution.

- `scripts/run_ai_advisory_packet.py`
  - Builds `data/ai_advisory_packet_latest.json` from current local diagnostics.
  - Can validate an AI response JSON and append it to `data/ai_advisory_decisions.jsonl`.

## Initial Playbooks

- `btc_b_failed_reclaim_ask_heavy`
  - Current BTC paper candidate.
  - BTC side=B, failed-reclaim continuation, top-book imbalance `ask_heavy`.

- `hype_b_range_scalp_research`
  - Research-only HYPE B-side range scalp.

- `eth_rejected_collect_only`
  - ETH remains rejected until diagnostics change.

- `alts_collect_only`
  - SOL/DOGE/BNB/xyz:GOLD/xyz:SILVER remain data/research only unless scope changes.

## Why This Fits Slim's Thesis

Quant-only systems can miss contextual shifts. AI can add value where the data is marginal, noisy, or contradictory:

- identify when the tape is changing character
- explain why indicators disagree
- suggest stand-down when news/regime risk is abnormal
- propose which deterministic sweep should run next
- keep BTC swing-style trades aligned with broader conditions

The constraint is that AI advice must be replayable and auditable. If it cannot be logged, validated, and compared against paper/live outcomes, it does not enter the bot.

## Next Build Step

Wire a daily/news context writer into the packet's `news` field, then run a cheap model such as MiniMax/Marvis over the packet to produce advisory JSON. Codex reviews the advisory output and only promotes stable patterns into deterministic code after paper validation.

## Claude Model Bake-Off

When Claude is used for advisory review, test **Sonnet first**. Slim read that Sonnet performs well with trading-style reasoning, so treat it as the first candidate rather than defaulting to Opus.

Comparison protocol:

- Run the same `data/ai_advisory_packet_latest.json` through Sonnet and Opus.
- Require each model to return the same advisory JSON schema with `model_id`.
- Validate both through `scripts/run_ai_advisory_packet.py --response-json <file>`.
- Compare them with `scripts/compare_ai_advisory_models.py`.
- Log whether Sonnet or Opus gave the cleaner, more usable advisory output.

Important: compare advisory quality first, not confidence theater. A good output is specific, evidence-backed, guardrail-clean, and willing to say `stand_down` when the setup is marginal.

# Thesis — `funding_neg_fade`

**Status:** research-only · **Written:** 2026-08-25 · **Lane:** `funding_neg_fade`
**Scored by:** `scripts/graduation_scorecard.py` against `docs/` graduation ladder
**Data:** venue-aligned panels only (see AGENTS.md §7 "Data integrity")

---

## 1. The claim as originally stated — and why it is wrong

The attested rationale (kslim, 2026-08-24) was:

> *negative funding pays the long side to hold; fading it is the carry side of
> the trade, not a fitted parameter*

**The measured P&L does not support this.** Decomposing all 37 admitted closed
trades on venue-aligned data:

| component | amount | share |
|---|---|---|
| net P&L | **+$117.82** | — |
| price move | +$142.56 | 121% |
| funding collected | **+$0.42** | **0.36%** |
| costs | −$25.16 | −21% |

In **0 of 37** trades did collected funding outweigh the price move. Costs are
**60× larger** than all funding collected.

The arithmetic was always going to say this. A median trade holds 4h at a median
entry funding of −7.7e−06 on ~$787 notional:

```
4h × 7.72e-06 × $787 = $0.024 of carry
median |net P&L| per trade = $6.38
```

**Carry is ~1/263 of a typical outcome.** It is a rounding error, not a
mechanism. At hourly funding rates near 1e−05 you would need to hold for weeks
for carry to matter, and the lane exits in hours.

## 2. The thesis that actually fits the data

**Negative funding is a positioning signal, not a payment.** When funding goes
negative, shorts are paying longs — which means the book is short-crowded.
The edge being captured is the **price reversion after short-crowding**, i.e. a
bounce. Funding is the detector; the money comes from direction.

This reframing matters because it changes the risk:

- A carry trade earns while it waits and is roughly direction-neutral.
- **This is a directional long.** It earns only if price rises, and it can be
  wrong in exactly the way any long can be wrong.

Supporting evidence: exits are 22 `max_hold`, 12 `take_profit`, 3 `stop_loss`.
A carry trade would want long holds; this one is mostly timing out, meaning the
bounce either happens quickly or does not happen.

## 3. What the clean numbers say

Venue-aligned, with the live position cap applied:

| lane | n | PF | win | regimes | verdict |
|---|---|---|---|---|---|
| HYPE | 16 | 1.48 | 62% | 1 (`high_vol_cascade` 15/16) | fails PF ≥ 1.5 |
| SOL | 11 | 8.53 | 91% | 4 | n < 15; small-sample |
| ETH | 10 | inf | 100% | 4 | n < 15; 7 fwd trades, no losers |
| BTC | 8 | 1.47 | 50% | 2 | fails n and PF |

**No lane clears Gate 1.** Every stronger figure previously reported was
inflated by a look-ahead bug (`docs/CHANGELOG.md`, 2026-08-25).

HYPE fires almost exclusively in `high_vol_cascade` — consistent with the
short-crowding reading, since cascades are where crowding gets extreme. Its 16
trades span **5 distinct episodes over 17 days**, so it is not one lucky window,
but episode-level n is 5, not 16. Trades inside one cascade are not independent.

## 4. Falsifiers — what would make us cut this

Written in advance so cutting is a decision, not a mood.

1. **PF stays < 1.5 after n ≥ 30** on venue-aligned data with the position cap
   applied → **cut the lane.** This is the primary test.
2. **Testnet PF is materially worse than paper** at matched n → the edge does
   not survive real fills and spread → cut.
3. **Reversion is regime-conditional and the regime is rare.** If HYPE produces
   no trades outside `high_vol_cascade` after 60 more days, it is a
   cascade-only lane: keep it only if it clears PF ≥ 1.5 on its own and accept
   that it will sit idle for weeks at a time.
4. **Signal frequency collapses.** Zero qualifying hours since 2026-08-20
   (confirmed against HL `fundingHistory`). If a 90-day window yields < 20
   qualifying hours per symbol, the lane cannot reach n ≥ 30 in any useful time
   → shelve regardless of PF.
5. **Costs dominate.** Costs are already 21% of gross. If cost/gross exceeds
   ~40% at live size, the edge is too thin to trade at this bankroll.

## 5. What must not be done to rescue it

- **Do not loosen `NEG_THRESHOLD` to manufacture trades.** That fits the gate
  instead of passing it. The drought is real, not a threshold artifact — the
  venue shows zero hours below *zero* since 2026-08-20.
- **Do not blend lanes.** Per the ladder, each asset is scored alone.
- **Do not re-derive funding from snapshots.** That is the look-ahead bug.

## 6. Open question for kslim

The attestation on file says the mechanism is carry. The data says the mechanism
is post-crowding reversion. These are different trades with different risk
profiles. **The attestation should either be revoked and re-made on the
reversion thesis, or the thesis rejected** — leaving a gate attested on a
mechanism that contributes 0.36% of returns is exactly the kind of unexamined
assumption the ladder exists to catch.

```
python3 scripts/graduation_scorecard.py --revoke HYPE:funding_neg_fade:logic_makes_market_sense
python3 scripts/graduation_scorecard.py --revoke SOL:funding_neg_fade:logic_makes_market_sense
```

## 7. Reproduce

```bash
python3 scripts/build_funding_from_venue.py
python3 scripts/build_candles_from_venue.py
python3 scripts/panel_health.py            # must pass before trusting anything
python3 scripts/paper_funding_neg_fade.py --mode paper
python3 scripts/graduation_scorecard.py
```

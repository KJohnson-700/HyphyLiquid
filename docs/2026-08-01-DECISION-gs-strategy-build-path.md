# Gold/Silver Hyperliquid Bot — Build Path Decision

**Date:** 2026-08-01
**Status:** Awaiting final decision (pending Claude review)
**Project repo:** `https://github.com/KJohnson-700/HyphyLiquid`
**Local workspace:** `C:\Users\AbuBa\Desktop\HyphyLiquid`
**Vault:** `C:\Users\AbuBa\Documents\Obsidian Vault\`

---

## TL;DR

Build the G/S Hyperliquid bot in **4 phases**, starting with **G/S ratio mean reversion** (not SMC, not ML). Add the **liquidation cascade** edge in Phase 2 — this is the unique Hyperliquid moat. Layer SMC as a *filter* in Phase 3, not a standalone strategy. Save ML for Phase 4 once we have 100+ of our own live trades.

**Key disagreement with Hermes:** the destination is right (cascade is the real edge), but the *starting strategy* is wrong. Start with ratio mean reversion because it exercises the full Hyperliquid stack without the MT5 port, and is fully market-neutral.

---

## 1. Background

We picked the **G/S Hyperliquid** project over SPX 0DTE in a head-to-head comparison (5/6 dimensions in favor — `projects/2026-08-01-PROJECT-COMPARISON-spx-vs-gs.md`). The project workspace and GitHub repo are both empty as of today:

- Repo: `https://github.com/KJohnson-700/HyphyLiquid` ("Testing Gold and Silver Strategies")
- Local: `C:\Users\AbuBa\Desktop\HyphyLiquid`

Hermes (research side) ranked three candidate strategies in `2026-08-01-TOP-3-STRATEGIES.md`. Mavis (build side / this assistant) reviewed and pushed back on the recommended starting strategy. This document is the consolidated decision package for Claude's review.

---

## 2. Top 3 Strategies (Hermes research)

| # | Strategy | Source repos | Verified backtest | Build difficulty |
|---|----------|--------------|-------------------|------------------|
| 1 | XGBoost + SMC + HMM regime filter | GifariKemal/xaubot-ai (60★) + 100★ merge forks | 63.9% WR, 2.64 PF, 2.2% max DD, 1-year | **High** (MT5 → Python rewrite) |
| 2 | Liquidation Cascade Countertrade | WickHunter (31★, 16 forks) | BTC $19B Oct '25, ETH $200M Jan '26 cascades | **Medium** (Hyperliquid-native) |
| 3 | SMC + Scalping + Macro filter (London-NY) | 3aLaee/xauusd-trading-bot (136★) + a1shmuk (5★) | 61.4% WR, 2.48 PF, 2-year | **Medium-low** (MT5 → Python) |

**Hermes's combined-score winner:** #3 SMC (score 12) over #1 (17) and #2 (18). Lower is better.

**Full technical breakdowns (Hermes):**
- `projects/gold-silver-hyperliquid/strategies/smc-scalping.md`
- `projects/gold-silver-hyperliquid/strategies/liquidation-cascade.md`
- `projects/gold-silver-hyperliquid/strategies/ml-approaches.md`

---

## 3. Hermes's Recommended Build Path

- **Phase 1 (Wks 1-4):** SMC Scalping — fork 3aLaee, adapt MT5 → Hyperliquid Python SDK, add London-NY overlap filter, validate 30-50 trades on testnet, capital $0 → $500
- **Phase 2 (Wks 5-8):** Liquidation Cascade — add OI divergence + funding rate filter + liquidation cluster tracker, 30 live trades, capital $500 → $1,000
- **Phase 3 (Wks 9-12):** Combine #3 + #2 with HMM regime filter, 50+ trades both strategies running
- **Phase 4 (Wks 13-20):** ML Enhancement — fork xaubot-ai, train on 100+ own trades, scale to $2-5k bankroll

---

## 4. Mavis Review — What I Agree With

- **Phased approach is correct.** Simple → validated → complex is exactly how you should build a trading bot.
- **Liquidation cascade is the unique Hyperliquid edge.** On-chain liquidations, OI, and funding rates are public on Hyperliquid and private everywhere else. That's a real, defensible moat — no CEX bot can replicate it. This is the right *destination*.
- **Testnet-first with $0 risk** to learn gold's intraday behavior is wise.
- **Forks as starting references** (xaubot-ai, WickHunter) rather than pure from-scratch.

---

## 5. Mavis Review — Where I Push Back

**1. The MT5 port is a much bigger lift than it sounds.**
3aLaee (136★) and GifariKemal (60★) are both **MQL5**, not Python. "Fork and adapt" is really "rewrite in Python while reverse-engineering someone else's strategy logic." That's a 3-4 week project minimum, not a fork job.

**2. SMC is harder to code than it looks.**
Smart Money Concepts (order blocks, FVG, BOS) is a *visual* methodology. Most public SMC bots are fragile, curve-fit, and break the first time market regime shifts. It's a poor *first* strategy to learn on because you can't tell if the bot is broken or the strategy is broken when something goes wrong.

**3. "Verified backtest" needs a grain of salt.**
The 61.4% WR / 2.48 PF / 2-year backtest doesn't include:
- Slippage on your actual fills
- Funding rate payments (8h apart, can erase thin edges)
- Live execution failures (order rejects, rate limits)
- The 30-50% out-of-sample decay that almost always happens
Treat those numbers as an *upper bound*, not a base case.

**4. Capital ramp is aggressive.**
$500 → $1,000 in 4 weeks → $2-5k in 20 weeks means you're banking on roughly doubling the account per month. Possible, but if it doesn't happen, you'll be tempted to override the bot. Better to plan for flat or modest gains and treat faster growth as a bonus.

**5. Combining two unproven strategies (Phase 3) is risky.**
If both strategies are running and the account is down, you won't know which one to blame. Each strategy needs ~50+ of *your own* live trades before you can judge it independently. Don't combine until then.

**6. Missing piece — no risk framework defined in either plan.**
Neither plan addresses:
- Max % account risk per trade
- Max leverage cap
- Daily loss kill switch
- Position correlation limits
- Funding rate P&L tracking (the silent killer)

Trading bots without a hard-coded risk module blow up. Period.

---

## 6. Mavis's Recommended Plan (revised phasing)

### Phase 1 (Wks 1-3): Foundation + G/S Ratio Mean Reversion
- Scaffold repo (Python venv, dirs, .gitignore, requirements, README, .env.example)
- Hyperliquid data layer — testnet auth first, prove SDK connection
- **G/S ratio mean reversion** in pure Python, native to Hyperliquid — no MT5 to port, market-neutral, works in 1-2 weeks
- Backtest 6-12 months historical candles
- Paper trade testnet 1 week
- Go live with $50 (not $500) as a real-money canary
- **Goal:** prove the *bot infrastructure* works (auth, orders, fills, funding accounting, logging) on a simple strategy
- **Capital:** $0 → $50

### Phase 2 (Wks 4-7): Liquidation Cascade (the unique edge)
- Build on-chain liquidation cluster detector + OI divergence + funding rate filter
- This is where the *real* Hyperliquid alpha lives
- Add as 2nd strategy in the same bot (multi-strategy framework)
- Paper trade 1 week, then live with $100
- **Capital:** $50 → $200

### Phase 3 (Wks 8-10): Simple SMC Filter (not standalone)
- Build *basic* SMC primitives in Python natively — **not** porting from MQL5
- One order block detector, one FVG detector
- Use as a **filter** on top of cascade + mean reversion signals, not a standalone strategy
- Pattern: "Only take cascade long if SMC structure is bullish"

### Phase 4 (Wks 11+): ML if justified
- Need 100+ of *your own* live trades before any ML is meaningful
- XGBoost on regime features
- Only add if simpler strategies are profitable

---

## 7. Risk Framework (apply to ALL phases)

Hard-coded rules that override everything else. Build `risk.py` in Week 1, before any strategy code.

- Max **1-2%** account risk per trade
- Max **5x** leverage (ignore the 50x available — leverage is an enabler at $500, a killer at 50x)
- Daily loss kill switch at **5%** → flat until next day
- Position correlation limits (no 3 long-correlated trades simultaneously)
- Funding rate P&L tracked daily (every 8h, can wipe thin edges)
- Reduce-only on every closing order
- No orders outside configured trading hours (or full 24/7 with explicit config)

---

## 8. Answering Hermes's Direct Question

> "Want me to update the Codex pathway to start with #3 instead of #2, or build a new hybrid implementation doc?"

**Neither. Rewrite the pathway to start with G/S ratio mean reversion (Phase 1), then add liquidation cascade (Phase 2).** The MT5 SMC port should be a Phase 3 *filter*, not a starting point. Cascade remains the destination, but mean reversion is the right on-ramp because it validates the infrastructure cheaply and quickly.

---

## 9. Open Questions (for Claude to weigh in on)

1. **Confirm the plan** — Mavis's 4-phase version, Hermes's original, or a hybrid?
2. **Hyperliquid wallet status** — does Slim have one ready, or is it testnet-only for the first 1-2 weeks?
3. **Live deployment target** — same Windows machine (`C:\Users\AbuBa\Desktop\HyphyLiquid`), or VPS/cloud for 24/7 uptime?
4. **Capital schedule** — comfortable with the conservative ramp ($50 → $200 → $500+), or push for faster scaling?
5. **Multi-strategy framework** — Phase 2 introduces a second strategy; agree with single-bot, multi-strategy design, or want separate bots per strategy?

---

## 10. Source Documents (to be created in vault)

- `projects/gold-silver-hyperliquid/notes/2026-08-01-CODEX-PATHWAY.md` — original implementation handoff (Hermes, Phase 1 = cascade)
- `projects/gold-silver-hyperliquid/notes/2026-08-01-TOP-3-STRATEGIES.md` — Hermes strategy ranking
- `projects/gold-silver-hyperliquid/strategies/smc-scalping.md` — Hermes deep-dive #3
- `projects/gold-silver-hyperliquid/strategies/liquidation-cascade.md` — Hermes deep-dive #2
- `projects/gold-silver-hyperliquid/strategies/ml-approaches.md` — Hermes deep-dive #1
- `projects/2026-08-01-PROJECT-COMPARISON-spx-vs-gs.md` — G/S vs SPX head-to-head
- `projects/gold-silver-hyperliquid/INDEX.md` — project index
- `projects/INDEX.md` — master vault index

---

## 11. Reviewer Notes

- **Mavis role:** Build / engineering — focuses on what's actually shippable in the timeline, what the technical risks are, what the MT5 port actually costs, where strategies break in live trading.
- **Hermes role:** Research / strategy — focuses on what backtests show, what's been validated elsewhere, what's the unique edge per venue.
- **Claude (you):** Asked for a third opinion on the consolidation. Look for: missing risks, alternate strategy starts, sequencing questions, things both Mavis and Hermes might be blind to.

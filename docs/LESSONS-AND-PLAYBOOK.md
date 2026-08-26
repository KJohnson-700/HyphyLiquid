# Lessons & Playbook

What this project learned the hard way, and how to use what it built.
Written 2026-08-26. Read this before trusting any number in this repo.

---

## Part 1 — The one rule

> **Read the venue, not our own disk. Measure the metric, not the exit code.**

Nearly every false result in this project traced to one of those two.

---

## Part 2 — Measurement traps found (all of these were live)

### 2.1 Look-ahead in the funding panel ⚠️ worst one

`asset_ctx.funding` is the rate for the **upcoming** settlement. Every builder
stamped it with `date_trunc('hour', poll_ts)`, putting each hour's funding on
the **previous bar**. The simulator traded a rate the venue had not published.

Caught by correlating our panel against HL `fundingHistory` at each time shift:

| shift | BTC | ETH | HYPE | SOL | mean |
|---|---|---|---|---|---|
| 0h | 0.913 | 0.540 | 0.408 | 0.678 | 0.635 |
| **+1h** | 0.973 | 0.944 | 0.891 | 0.975 | **0.946** |

Every symbol peaked at +1h. **HYPE's PF fell 2.34 → 1.48 once corrected.**

- ✅ `build_funding_from_venue.py`, `build_candles_from_venue.py`
- ❌ **never in a loop:** `build_funding_panel.py`, `build_panels_from_duckdb.py`
- 🔍 `panel_health.py` checks alignment, is a **blocking** daemon step

### 2.2 Stale local sources (found four times)

Local files look populated but aren't representative. Each of these silently
produced wrong answers:

| reader | was reading | should read |
|---|---|---|
| funding panel | `asset_ctx` jsonl (deleted by maintenance) | venue |
| `label_trade_regimes` | `*_candles_1h_90d_*.csv`, few symbols | `candle_panel.csv` |
| `load_hl_recent` | `ws_candle/` — ~3 days | `candle_panel.csv` |
| fade daemon | jsonl + DuckDB builders | venue builders |

The daemon case was worst: it re-corrupted the panel hourly *after* the fix,
alignment falling 1.0000 → 0.8538 overnight, every step logging `ok`.

### 2.3 Level vs edge triggering

`signal_funding_neg_fade` returns 1 for **every bar** funding is below
threshold. Entering on the level re-opens the same episode after each close —
acting on hours-old information already in the price.

```
ETH   edge n= 88 PF 1.65      level n=111 PF 1.04
HYPE  edge n= 87 PF 1.70      level n= 90 PF 1.66
```

ETH's negative stretches are long, so it re-entered 23 times, all losers.
HYPE's are short, which is why this hid for weeks. **A signal is an event.**

### 2.4 The simulator held positions the exchange would refuse

It walked each symbol independently — four lanes open at once for 11 hours,
against `RiskConfig.max_open_positions = 3`. Replaying against the real cap
removed **21% of trades and 44% of net profit**.
→ `src/strategy/position_cap.py`, applied in the scorecard.

### 2.5 Random trade IDs duplicated the record

`paper_id` embedded 6 chars of `secrets.token_hex`, so re-walking the panel
minted a new id for a trade already on disk. **66 rows held 57 real trades**,
inflating n/PF/win — the exact gate inputs. BTC read PF 1.49 against a 1.5 gate
purely from double counting.
→ ids are now deterministic on `(symbol, entry bar)`.

### 2.6 The regime classifier measured the asset, not the market

`classify_candle_regime` overrides every label at `atr_pct >= 0.50` — an
**absolute** threshold. Run bar-by-bar with no trades involved:

```
HYPE high_vol_cascade 100%   ZEC 100%   ETH 83%   BTC 65%
```

HYPE is never calm enough to fall under it, so the label carried zero
information. **This produced a completely false conclusion** — "HYPE's edge is
cascade-only, 85/85 trades" — that nearly got the lane cut.
→ threshold now calibrated per asset at its own 80th ATR percentile.
HYPE relabelled: **4 regimes**, not 1.

### 2.7 `no_data` counted as a regime

Gate 2 wants ≥2 regimes. HYPE had 29 cascade + 2 unlabelled and was **passing
on the 2 unlabelled ones**. → `NON_REGIMES` filters them.

### 2.8 Small-sample p-hacking

A 25-day hold sweep produced **PF 11.55 at n=16** — the best of 24
configurations on ~30 trades. On 7 months every config landed 0.86–1.25.

> **Rule adopted:** a config is accepted only if it clears the gate in **both
> independent halves** with ≥25 trades each. 648 swing configs tested, 1 survived.

### 2.9 Cohort selection was the entire whale signal

Of the top 30 leaderboard accounts by each key, the number holding any position:

```
accountValue   3/30   ← vaults and idle treasuries
month pnl      3/30
month volume   9/30
month roi     21/30   ← default now
```

Ranking by size selects **capital, not traders**. The first run read 60
accounts, 57 held nothing, and reported "whales 100% short HYPE" off a single
position. The real cohort was 53 long / 1 short.

Also: **a deposit reads as a 438,185% monthly return**, and several top-ROI
accounts have $0 volume. Filter `0 < roi < 5` **and** a volume floor.

### 2.10 `userFills` caps at 2000

On an active trader that was **4.2 days**. Paging `userFillsByTime` got 4,737
fills over 29.9 days for the same account. Hold times off the capped window
describe last week, not the strategy.

### 2.11 Exit code ≠ progress

The fade daemon ran **11 hourly ticks logging `ok` on every step** while
`Total closed: 44` never moved. Watch the metric the loop exists to move.

Corollary: **distinguish idle from stalled.** No qualifying signal is correct
behaviour; signals present with no trade is a fault. Warning on the first
trains you to ignore the second. And an unknown must never read as healthy —
helpers return `-1`/skip on unreadable input, never 0.

### 2.12 Tests wrote into production logs

`test_no_pairs` repointed three paths but not `LOG_PATH`, so pytest output
interleaved with production in `logs/l2_cascade_features.log`.
→ `tests/conftest.py` redirects the whole suite via `HYPHYLIQUID_LOG_DIR`.

---

## Part 3 — What is actually true about the market

### 3.1 The decisive arithmetic

```
                edge/trade   round-trip cost   ratio
cascade lane      0.030%         0.090%        0.3x   ← dead
fade lane         0.719%         0.080%        9.0x
```

**Costs are a fixed toll.** The only ways to beat them are bigger moves or
fewer trades. Frequency does not rescue a sub-cost edge — 1,600 signals/day at
negative expectancy loses money 1,600 times faster. This one ratio explains
every dead lane in this project.

### 3.2 Holding period is structural, not a parameter

Measured across **349 profitable Hyperliquid accounts** (`analyze_whale_fills.py`):

```
band       n    winners   median PF
<2h       33      61%       1.67
2-8h      38      53%       1.33   ← the fade lane's old 4h config
8-24h     72      64%       1.97
24-72h    89      74%       2.63
>72h      67      72%       2.82
```

swing 72% vs intraday 51%: **z = 2.38, p = 0.017**.

The old HYPE config sat in the worst band on the venue. Moving to 24h **and**
widening stops 3× took it from ~0.90 to 1.70. *Either change alone leaves it
under the gate.*

### 3.3 What is dead, with sample sizes

| lane / strategy | n | net PF | verdict |
|---|---|---|---|
| liquidation cascade (all variants) | 826 | 0.47 | cut |
| `funding_carry` | 199 | 0.62 | cut |
| `funding_max_fade` | 80 | 0.70 | cut |
| grid (Chainstack) | 90–170 | 0.74–1.02 | cut |
| `ma_cross` | 47–108 | 0.77–1.03 | cut |
| `bb_squeeze` | 22–34 | 0.50–1.11 | cut |
| RWA (`xyz:*`) all strategies | 26–103 | 0.31–1.72 | cut |

Note `funding_carry` losing at n=199 while `funding_neg_fade` wins: **the
mechanism is not carry.** Decomposed, funding is 0.36% of net P&L, and costs
are 60× all funding collected. The edge is price reversion after
short-crowding; funding is the *detector*, not the payment. That makes it a
**directional long**, not a market-neutral carry trade.

### 3.4 What survives

```
HYPE  funding_neg_fade  n=88   PF 1.71  4 regimes   H1 1.87 / H2 1.56
ETH   funding_neg_fade  n=90   PF 1.69  5 regimes   H1 1.68 / H2 1.59
ZEC   swing             n=109  PF 1.70  4 regimes   H1 1.95 / H2 1.57
```

All three found on **venue-sourced history**. Everything found on local capture
evaporated when measured properly.

### 3.5 Signal scarcity is real

Funding qualifying hours are ~2–4% of the time and **bursty** — HYPE went from
16 in one week to zero for the next six days. The venue confirmed zero hours
below zero across all symbols 08-20 → 08-25. A drought is the market, not a
bug — but verify it against `fundingHistory` before believing it.

---

## Part 4 — Free data most bots don't use

Hyperliquid publishes on-chain what centralized venues keep private. **This
transparency is the moat**, and none of it needs a key.

| what | how | we collect it |
|---|---|---|
| leaderboard, 43,674 accounts w/ PnL/ROI/volume | `stats-data.hyperliquid.xyz/Mainnet/leaderboard` | hourly |
| any address's positions | `clearinghouseState` | hourly |
| any address's fill history | `userFillsByTime` (paged) | on demand |
| any address's resting orders + stops | `frontendOpenOrders` | hourly |
| full candle history (~5,000 bars) | `candleSnapshot` | on demand |
| full funding history | `fundingHistory` | on demand |

Sample of what that yields: BTC carrying **$83M of bid support** from
profitable wallets vs $25M resistance; one wallet's $11.4M bid at −3.58% with
its stop-ask visible at −7.02% — entry and stop both legible.

**Paid alternatives** (QuickNode gRPC `StreamTpslUpdates`, `StreamL4Book`) add
real-time + complete venue coverage. Worth revisiting **only** if forward
testing shows a signal works and coverage is what binds. Don't buy resolution
on an unvalidated signal.

---

## Part 5 — How to use what's built

### Daily / operational

```bash
python3 scripts/panel_health.py          # ALWAYS before trusting a result
python3 scripts/graduation_scorecard.py  # where every lane stands
python3 scripts/graduation_scorecard.py --show-attestations
tmux ls                                  # 7 daemons expected
```

### Rebuild data from the venue

```bash
python3 scripts/build_funding_from_venue.py --since 2026-01-29
python3 scripts/build_candles_from_venue.py --since 2026-01-29
python3 scripts/label_trade_regimes.py    # all lanes, per-asset ATR threshold
```

### Research a strategy

```bash
python3 scripts/sweep_fade_hold.py        # hold × stop-width grid
python3 scripts/swing_lane.py             # both-halves selection, 648 configs
python3 scripts/strategy_search.py --strategy all --symbol HYPE --source hl-funding
```

### Mine profitable traders

```bash
python3 scripts/analyze_whale_fills.py --top 400 --days 30
python3 scripts/whale_strategy_patterns.py --min-closes 30
```

### Testnet execution (ARMED as of 2026-08-26)

```bash
python3 scripts/paper_funding_neg_fade.py --mode testnet_trading            # dry
python3 scripts/paper_funding_neg_fade.py --mode testnet_trading --arm-testnet
```

Refuses unless `HYPERLIQUID_ENV=testnet`, re-checks the URL, honours the kill
switch, enforces the position cap, refuses signals older than one bar.

### The daemons

```
poller paper ws liq ctx   collectors
fade                      venue panels → health gate → fade → swing → testnet → regimes
whale                     leaderboard → positions + resting orders/stops
```

Kill switch: `touch data/live_kill_switch.flag`

---

## Part 6 — Open work

1. **HYPE is one gate from canary** — only `decision_path_has_tests` remains,
   and it clears when the armed testnet mode books its first real fill.
   Do **not** attest it on unit tests; the gate is about the live path.
2. **Cross-sectional funding rank.** Senpi's Camel ranks ~230 assets by funding
   and takes the most extreme, instead of thresholding two symbols. We have 20
   symbols × 7 months aligned. Untested, structurally different.
3. **Whale signals are collecting but unvalidated** — positioning, stop map,
   resting-order map. None has been shown to predict anything. Needs 2–3 weeks,
   then test whether cluster density or skew *changes* lead price.
4. **ETH needs `logic_makes_market_sense`** — it clears every numeric gate.
5. **The 4h trend gate** (from Camel) helps HYPE (H2 1.56→1.69) and hurts ETH
   (H2 →1.19). Per-asset at best; within noise at n=53–87. Not adopted.

---

## Part 7 — Checklist before believing any new result

- [ ] `panel_health.py` passes — coverage **and** venue alignment
- [ ] n is in the hundreds, not tens
- [ ] Clears the gate in **both halves** independently
- [ ] Costs applied (8bps round trip)
- [ ] Position cap applied
- [ ] Top trade < 40% of gross profit
- [ ] Entry is edge-triggered, not level-triggered
- [ ] How many configurations were tried before this one?
- [ ] Compared against a control (losers, buy-and-hold, base rate)
- [ ] Does the mechanism explain the P&L? (carry didn't — it was 0.36%)

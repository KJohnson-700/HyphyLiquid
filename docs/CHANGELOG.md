# HyphyLiquid changelog

Findings and decisions that change how results should be read. Newest first.
Code detail lives in commit messages; this records *why* and *what it means for
the numbers*.

---

## 2026-08-26

### The fade lane was recalibrated, not cut

Ran it one more time before cutting, at 24-72h holds, on the venue's full
history (5,002h/symbol) rather than 25 days. Two changes were both required --
hold 4h -> 24h and stops x3 -- and the result holds in independent halves:

  HYPE  n=88  PF 1.71  4 regimes   H1 1.87 / H2 1.56
  ETH   n=90  PF 1.69  5 regimes   H1 1.68 / H2 1.59
  ZEC   swing lane, n=109 PF 1.70  H1 1.95 / H2 1.57

BTC and SOL fail in both halves and were removed from PER_ASSET_POLICY.

### Four more measurement bugs

- **Level vs edge triggering.** Entries fired on every bar funding sat below
  threshold, re-opening the same episode after each close. ETH: level n=111
  PF 1.04 vs edge n=88 PF 1.65.
- **The regime classifier measured the asset, not the market.** An absolute
  `atr_pct >= 0.50` labelled 100% of HYPE and ZEC bars as high_vol_cascade,
  traded or not. This produced a false conclusion -- "HYPE's edge is
  cascade-only" -- that nearly cut the lane. Per-asset percentile now; HYPE has
  4 regimes.
- **`no_data` counted as a regime**, satisfying Gate 2 on unlabelled trades.
- **Stale sources, twice more:** the regime labeller and `load_hl_recent` were
  reading narrow local files instead of the panel. Every price strategy had
  been judged on ~3 days.

### What is dead, with real sample sizes

cascade 0.47 (n=826), funding_carry 0.62 (n=199), funding_max_fade 0.70 (n=80),
grid 0.74-1.02 (n=90-170), ma_cross 0.77-1.03, bb_squeeze 0.50-1.11, all RWA.

`funding_carry` losing at n=199 while `funding_neg_fade` wins showed the
mechanism is not carry: funding is 0.36% of net P&L and costs are 60x all
funding collected. The edge is reversion after short-crowding.

### Free venue data now collected

Leaderboard (43,674 accounts), any address's positions, fill history, and
resting orders/stops -- all no-auth. 349 profitable traders were fingerprinted;
the 2-8h holding band is the worst on the venue (53% winners) against 24-72h
(74%), z=2.38 p=0.017.

### Testnet execution armed

HYPE now clears every data-driven gate. Only `decision_path_has_tests` remains
and it is deliberately unattested -- unit tests do not cover the live path.

See `docs/LESSONS-AND-PLAYBOOK.md`.

---

## 2026-08-25

### Panels now come from the venue, not from local snapshots

`asset_ctx.funding` is the rate for the **upcoming** settlement, but every
snapshot-derived builder stamped it with `date_trunc('hour', poll_ts)`. That put
each hour's funding on the previous bar, so the simulator traded on a rate
Hyperliquid had not published yet — **look-ahead bias**.

Found by correlating our panel against the venue's own `fundingHistory` at each
time shift:

| shift | BTC | ETH | HYPE | SOL | mean |
|---|---|---|---|---|---|
| 0h | 0.913 | 0.540 | 0.408 | 0.678 | 0.635 |
| **+1h** | 0.973 | 0.944 | 0.891 | 0.975 | **0.946** |

Every symbol peaked at +1h.

- **Use:** `scripts/build_funding_from_venue.py`, `scripts/build_candles_from_venue.py`
- **Do not use in any loop:** `build_funding_panel.py`, `build_panels_from_duckdb.py`
- **Check:** `scripts/panel_health.py` verifies coverage *and* alignment, and is
  a blocking step in the fade daemon — a failure aborts the tick so the strategy
  never runs on a bad panel.

Both venue endpoints serve history on demand, so panels are reproducible from
scratch and **local jsonl retention is no longer on the critical path**.
Coverage went from ~174 locally-captured hours to 583 (from 2026-08-01).

> The snapshot builders were still in the daemon after the first fix and
> silently re-corrupted the panel every hour — alignment fell 1.0000 → 0.8538
> overnight while every step logged `ok`.

### Collection stopped for channels nothing reads

~2.3 GB/day was being written to directories no running process consumed.
Disabled `bbo` and `activeAssetCtx`; stopped the duplicate raw `ws_trades` write
(`data/trades` already held every trade — verified across all 45 files before
deleting). Kept `trades`, `l2Book`, `candle`, each with a named live consumer.
Re-enable via `ENABLED_CHANNELS` in `scripts/collect_ws_data.py`.

`data/ws_bbo` (1.5G) and `data/ws_activeAssetCtx` (654M) remain on disk —
collection is off, but neither is reconstructible.

---

## 2026-08-24

### The paper simulator was inflating results three ways

1. **No position cap.** It walked each symbol independently and held four lanes
   at once for 11 hours; `RiskConfig.max_open_positions = 3`. Replaying against
   the cap removed **12 of 57 trades (21%) and $42.95 — 44% of net profit**.
2. **Duplicate trades.** `paper_id` embedded random hex, so re-walking the panel
   re-appended trades already on disk. 66 rows held 57 real trades, inflating
   n/PF/win rate — the exact gate inputs. BTC read PF 1.49 against a 1.5 gate
   purely from double-counted winners.
3. **Look-ahead funding** (see above).

A simulator cannot catch this class of error; a real fill can. Hence
`--mode testnet_trading`, which routes the same signals to Hyperliquid testnet.
**Unarmed by default** — needs `--arm-testnet`.

### Latent crash in the mainnet path

`mode_live_trading` did `sig[i]` on a Series carrying a DatetimeIndex — a label
lookup that raises `KeyError` on an integer. The mainnet path would have crashed
on its first real signal. Never caught because the flag has always been off.

### Scorecard impact

| lane | before (misaligned, no cap) | after (venue-aligned + cap) |
|---|---|---|
| HYPE | PF 2.34, n=14 | **PF 1.48, n=16** — below the 1.5 gate |
| SOL | PF 1.08, n=12 | PF 8.53, n=11 |
| ETH | PF 4.94, n=10 | PF inf, n=10 (7 fwd trades, no losers) |
| BTC | PF 1.29, n=14 | PF 1.47, n=8 |

**No lane currently clears Gate 1.** HYPE's earlier edge was substantially the
look-ahead. SOL's 8.53 rests on n=11 at a 91% win rate — small-sample, not an 8x
edge.

### Attestations persist

`--attest` was in-memory only: a lane printed `FORWARD_PAPER` once and fell back
to `RESEARCH_ONLY` on the next bare run, so the ladder recorded no decisions.
Now stored in `data/attestations.json` with who/when/why, plus `--revoke` and
`--show-attestations`. Git-tracked via a `!data/attestations.json` negation.

`logic_makes_market_sense` attested for HYPE and SOL (kslim, 2026-08-24).

### Signal drought is real

Hyperliquid's `fundingHistory` shows **zero hours below zero** on any traded
symbol since 2026-08-20. Confirmed against the venue, not inferred from our
panel. No threshold change would produce trades.

---

## Operating rules learned the hard way

- **A daemon step exiting 0 is not progress.** Watch the metric the loop exists
  to move. An 11-hour stall reported `ok` on every step while the closed-trade
  count never moved.
- **Distinguish idle from stalled.** No qualifying signal is correct behaviour;
  signals present with no trade is a fault. Warning on the former trains you to
  ignore the latter.
- **An unknown must never read as healthy.** Helpers return `-1`/skip on
  unreadable input rather than 0.
- **Verify against the venue, not against our own capture.** Both the drought
  and the look-ahead bug were only settled by asking Hyperliquid directly.

# HyphyLiquid changelog

Findings and decisions that change how results should be read. Newest first.
Code detail lives in commit messages; this records *why* and *what it means for
the numbers*.

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

# HyphyLiquid — Changelog

> Running log of meaningful changes, fixes, and milestones. Most recent first.

---

## 2026-08-01 — Order Manager Hard-Parts Pass (Codex)

Codex reviewed `src/execution/order_manager.py` against the order manager review brief and patched the parts that can create real execution risk. Repo venv and pytest are healthy (101/101 pass). Details in `docs/2026-08-01-HANDOFF-order-manager-hard-parts.md` and the matching vault research note.

**Safety changes**
- `SignalDirection.NO_TRADE` is now rejected explicitly instead of being treated as a short.
- ATR fallback trading removed: empty/short candle history now refuses to size a position rather than synthesizing a stop distance.
- Default `bulk_orders` grouping switched from `positionTpsl` to `normalTpsl` (parent entry + TP/SL children). `positionTpsl` is back-burnered pending testnet proof.
- Orphan-entry handling added: if the entry lands but a child TP/SL errors, the manager attempts `bulk_cancel` on the entry, sets `status="orphan_error"`, and flags `needs_reconciliation=True`.
- `OrderResult` extended with `status`, `size_coin`, `needs_reconciliation`, `cancel_attempted`, per-leg statuses, and the cancel response.
- Size rounding prefers Hyperliquid `meta()` `szDecimals`; falls back to hardcoded BTC/ETH/SOL only if the call fails.

**Tests** (101/101 pass on the repo venv, 3 new + 1 renamed)
- `test_no_trade_signal_rejected_not_short`
- `test_refuses_to_trade_without_atr_history`
- `test_orphan_entry_attempts_cancel_when_child_order_fails`
- `test_bulk_orders_called_with_positionTpsl_grouping` → renamed to `..._normalTpsl_grouping`

**Files**
- `src/execution/order_manager.py`
- `tests/test_order_manager.py`
- `docs/2026-08-01-HANDOFF-order-manager-hard-parts.md` (durable handoff, mirrored to vault)

**Out of scope (per handoff "Mavis must not" list)**
- No mainnet orders, no risk-limit changes, no re-enable of ATR fallback, no revert to `positionTpsl` without testnet proof, no ML/DCA/martingale/new assets/venues.

---

## 2026-08-01 — Testnet vs Mainnet Reality Check (CRITICAL FINDING)

**The testnet backtest was a fiction. Mainnet data is fundamentally different.**

After fetching 90 days of mainnet data, the funding rate distribution is **100-200x smaller and 100x smoother** than testnet:
- Testnet BTC funding range: -0.296% to +0.704% per hour
- Mainnet BTC funding range: -0.0027% to +0.0019% per hour
- The 0.10% / -0.05% testnet thresholds NEVER fire on mainnet

**Mainnet backtest (90d, scale-appropriate thresholds):**
- Only 32% of 25 sweep configs profitable
- CoV 1.55 (UNSTABLE)
- Median return -4.28% (slightly negative)
- Best config: high=0.0020%, low=-0.0025%, +1.85% return on **6 signals** (not statistically meaningful)
- Going to lower thresholds (more signals) makes it WORSE — 160 signals = -13%, 2700 signals = -87%

**Directional check (full 2160 events, 100% matched):**
- 84% of funding events are positive (longs pay shorts) — this is just steady-state premium, not extreme positioning
- BTC drifted from $80K to $63K in 90d while 84% of funding was positive
- The "extreme high" funding event (>1.5e-5, 1 event): price went UP 154 bps over 24h
- The "extreme low" funding events (BTC < -1.5e-5, 28 events): price went DOWN 24 bps over 24h

**Implication: the simple "funding extreme = cascade signal" hypothesis is structurally wrong on this market.** Funding is too small, too constant, and follows trend rather than predicting reversal. The testnet edge was an artifact of synthetic chaotic funding.

**Where the real edge probably lives:** actual liquidation EVENTS, not inferred from funding. Hyperliquid publishes liquidation data on-chain. A signal based on real liquidations (price impact, volume signature, OI drop) is the next research direction.

**Code:**
- `scripts/fetch_historical.py` — `HYPERLIQUID_ENV` env var (testnet | mainnet); filenames now include env suffix
- `scripts/run_backtest.py` — `_find_data()` prefers mainnet > testnet, longest lookback first
- `scripts/mainnet_sweep.py` — mainnet-scale parameter sweep (NEW)
- `scripts/_funding_dist.py` and `scripts/_debug_loader.py` — debugging aids (can be removed)
- `scripts/_funding_vs_price_v2.py` — directional check (proves strategy is structurally wrong)
- Data: 90d BTC+ETH mainnet, 90d BTC+ETH testnet, 30d BTC+ETH testnet all in `data/`

---

## 2026-08-01 — 90-day Backtest Pass

- `scripts/fetch_historical.py` — added `import pandas as pd` (NameError fix); pagination walks 20-day chunks to bypass 500-event API cap
- `scripts/run_backtest.py` — `load_data()` now picks the longest lookback file available (90d > 30d > 7d)
- Fetched 90 days: 2161 candles + 2160 funding events per symbol (BTC + ETH)
- Backtest on 90d: 387 trades, 59.4% WR, PF 5.89, 100% of sweep configs profitable, walk-forward CONSISTENT (3/3 folds green)
- **Caveat: this is TESTNET data, not mainnet.** Funding dynamics may differ.
- See `vault/changelog.md` 2026-08-01 entry for full results

---

## 2026-08-01 — Public Research Sweep

- Added `docs/2026-08-01-RESEARCH-REPOST-btc-eth-liquidation.md` for AI-editor context.
- Reviewed public GitHub projects covering Hyperliquid liquidation alerts, wallet tracking, and trading infrastructure.
- Attempted Reddit, X, and YouTube searches; access/rendering limitations are recorded rather than presenting unverified claims as evidence.
- Decision unchanged: BTC/ETH only, Hyperliquid only, one cascade strategy, strict risk controls, and no ML before 100+ own live trades.



### Overview
Major pivot: dropped the gold/silver scope, leaned into the cascade edge on crypto. Also laid down the full project skeleton (code, tests, risk module) in the same day.

### Decisions made
- **PIVOT:** G/S Hyperliquid → **BTC/ETH liquidation cascade** on Hyperliquid. Reason: Week-0 market recon showed PAXG is the only metal perp on HL, too thin for a $500+ strategy (0.13% of BTC's daily volume, max 10x leverage). The cascade edge (the most defensible Hyperliquid moat) is a crypto phenomenon. See `vault/notes/2026-08-01-PIVOT-btc-eth-cascade.md`.
- **Single project, $1,000 bankroll.** Concentrate capital on the highest-EV play.
- **Conservative ramp:** $50 canary → $200 → $500 → $1,000 over 6-8 weeks.
- **Honest targets:** 12-15% monthly, 55-65% WR, 2.0+ PF.

### Code shipped
- `AGENTS.md` — scoped MD file for AI editors (Claude, Cursor, OpenCode, Codex, Aider, etc.)
- `README.md`, `.gitignore`, `.env.example`, `requirements.txt`
- `config/settings.example.yaml`
- `src/risk.py` — risk module with full test coverage
- `tests/test_risk.py` — 16 tests covering all 8 risk verdicts
- `src/{exchange,strategy,execution,journal}/__init__.py` — module skeletons

### Open
- [ ] First commit + push to GitHub
- [ ] Update GitHub repo description from "Testing Gold and Silver Strategies" to "BTC/ETH cascade bot on Hyperliquid"
- [ ] Week 1: testnet auth proof
- [ ] Decide: rename vault folder from `gold-silver-hyperliquid` to something crypto-themed?

---

## 2026-08-01 — Project Init (earlier same day)

- Project folder created on GitHub (`KJohnson-700/HyphyLiquid`) and local workspace
- Market recon script shipped (`scripts/market_recon.py`)
- Recon results saved to `scripts/market_recon_raw.json`
- Obsidian vault structure initialized

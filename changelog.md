# HyphyLiquid — Changelog

> Running log of meaningful changes, fixes, and milestones. Most recent first.

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

---
project: hyphyliquid
asset: crypto
strategy: btc-eth-cascade
venue: hyperliquid
status: building
date: 2026-08-01
---

# AGENTS.md — HyphyLiquid

> **AI Editor Instructions (Claude, Cursor, OpenCode, Codex, Copilot, Aider, etc.):**
> Read this file first. It defines the project scope, stack, conventions, and what is NOT in scope.
> **Do not invent your own patterns** — match what's documented here.
> When in doubt, ask Slim before deviating. When you find a bug, fix it (don't paper over).
> When you change something, update both the code changelog AND the vault changelog.

---

## TL;DR

We are building an automated **BTC/ETH liquidation cascade counter-trade bot** on **Hyperliquid mainnet** with a **$1,000 USDC bankroll**. Single project, single venue, single strategy class. Conservative risk rules. 4-week build to first live trade.

**This is NOT a gold/silver project.** That was the original framing and recon (Week 0) proved it unviable — PAXG is the only metal perp on HL and it's too thin to support a $500+ strategy. We pivoted to crypto on 2026-08-01. See `vault/notes/2026-08-01-PIVOT-btc-eth-cascade.md` for the full pivot rationale (vault path: `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\notes\`).

---

## 1. Scope (what we ARE building)

### Strategy
**BTC/ETH Liquidation Cascade Counter-trade** — detect on-chain liquidation events on Hyperliquid, enter counter-trades within a tight window, exit on mean reversion or stop loss. Reference: WickHunter (31★ on GitHub) for patterns.

### Venue
**Hyperliquid mainnet** (testnet first for auth proof, mainnet for live). Official Python SDK. Wallet-based auth, no KYC.

### Capital
- **Total bankroll:** $1,000 USDC
- **Risk per trade:** 0.5-1% = $5-$10
- **Leverage cap:** 10x (HL allows up to 40x on BTC, 25x on ETH; we cap ourselves at 10x)
- **Position size:** ~$5,000-$10,000 notional per trade
- **Ramp:** $50 paper canary → $200 live → $500 → $1,000 over ~6-8 weeks

### Targets (honest, not aspirational)
- **Win rate:** 55-65%
- **Profit factor:** 2.0+
- **Monthly return:** 12-15% (NOT 25-33% as earlier drafts claimed)
- **Trades per month:** 12-15
- **Time to first live trade:** 4 weeks from 2026-08-01

---

## 2. Out of Scope (what we are NOT building)

The following were considered and explicitly rejected. **Do not propose adding them unless Slim changes the scope.**

- ❌ **Gold/Silver ratio trading on Hyperliquid** — PAXG is the only metal perp; OI ~$22.9M, 24h vol ~$919K, max 10x leverage. Too thin. Pivoted away on 2026-08-01.
- ❌ **XAU/XAG perpetuals on other venues** — defeats the "Hyperliquid wins 5/6 dimensions" rationale (KYC, friction, second SDK).
- ❌ **MT5/MQL5 ports** — xaubot-ai, 3aLaee/xauusd-trading-bot etc. are MQL5, not Python. "Fork and adapt" is a 3-4 week rewrite, not a fork.
- ❌ **ML strategies before we have 100+ of our own live trades** — out-of-sample decay is real; no ML on synthetic backtests.
- ❌ **Second strategy in parallel from day 1** — single strategy, validate, then add. Don't diversify before we have data.
- ❌ **Leverage above 10x** — the 50x HL allows is a liquidation engine, not a tool.
- ❌ **PAXG as a standalone strategy** — too thin. May revisit as a pairs-trade component in Phase 2+, but not standalone.
- ❌ **Spot/AMM trading** — Hyperliquid perps only.
- ❌ **Polymarket-bot integration** — that's a different running project (see vault `projects/polymarket-bot/`). Don't cross the streams.

---

## 3. Stack

### Runtime
- **Python 3.10+** (confirmed available: 3.10.11 and 3.10.5)
- **venv** for isolation
- **PowerShell** on Windows

### Core dependencies
- `hyperliquid-python-sdk` — official SDK (1,769★)
- `pandas`, `numpy` — data
- `requests` — already installed, for recon-style calls
- `python-dotenv` — env vars
- `pyyaml` — config
- `ta` or `pandas-ta` — technical indicators (add when needed)
- `matplotlib` — backtest viz
- `pytest` — tests

### Don't add
- ❌ TensorFlow / PyTorch — no ML yet
- ❌ Web3.py — SDK handles wallet/auth
- ❌ Streamlit / Dash — no dashboard in v1
- ❌ CCXT — use the official HL SDK directly

---

## 4. Directory structure

```
HyphyLiquid/
├── AGENTS.md                ← you are here
├── README.md                ← human intro
├── .gitignore
├── .env.example
├── requirements.txt
├── config/
│   └── settings.example.yaml
├── data/                    ← gitignored, historical candles
├── logs/                    ← gitignored, runtime logs
├── notebooks/               ← analysis / exploration
├── scripts/
│   └── market_recon.py     ← already exists, run any time
├── src/
│   ├── __init__.py
│   ├── config.py            ← load settings + env
│   ├── risk.py              ← risk module (built FIRST, in Week 1)
│   ├── exchange/
│   │   ├── __init__.py
│   │   └── hyperliquid.py  ← SDK wrapper, auth, market data
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── cascade.py      ← BTC/ETH cascade strategy
│   ├── execution/
│   │   ├── __init__.py
│   │   └── order_manager.py
│   └── journal/
│       ├── __init__.py
│       └── trade_journal.py
└── tests/
    ├── __init__.py
    ├── test_risk.py         ← run FIRST
    └── test_exchange.py
```

---

## 5. Risk framework (HARD RULES — no exceptions)

Build `src/risk.py` in Week 1, before any strategy code. Every other module calls into it. The risk module overrides everything else. If `risk.py` says no, the answer is no.

- **Max risk per trade:** 1% of bankroll = $10
- **Max leverage:** 10x
- **Max open positions:** 3 (correlated assets count as 1)
- **Daily loss circuit breaker:** 3% = $30 → flat until next day
- **Weekly loss circuit breaker:** 5% = $50 → reduce size 50% for the rest of the week
- **3 consecutive losses** → halt 24h
- **Bankroll < $600 (40% drawdown)** → STOP, re-evaluate with Slim
- **No orders outside configured trading hours** (or full 24/7 with explicit config)
- **Reduce-only** on every closing order
- **Funding rate P&L** tracked daily (every 8h on HL)
- **All trades journaled** to `vault/journal/YYYY-MM-DD-trades.md` (Obsidian) AND `src/journal/trade_journal.py` (code)

---

## 6. Build phases

### Week 0 (this weekend) — Foundation
- [x] Project init (folder, GitHub repo)
- [x] Market recon — PAXG is the only metal perp, too thin → pivot to crypto
- [x] Second brain in Obsidian vault
- [x] This file (`AGENTS.md`)
- [x] Project skeleton (`.gitignore`, `requirements.txt`, `README.md`, dirs)
- [x] `risk.py` skeleton + tests
- [ ] **First commit + push to GitHub** ← do this after skeleton is in place

### Week 1 — Auth + data layer
- Hyperliquid testnet auth proof (place + cancel one order)
- Candle + funding history fetch and store
- Order manager skeleton

### Week 2 — Strategy
- Cascade detector (liquidation events, OI divergence, funding extremes)
- Backtest on 6-12 months of historical data
- Honest metrics (including slippage + funding)

### Week 3 — Paper + risk hardening
- Testnet paper trading 1 week
- Tighten risk module based on observed behavior
- Build dashboard / monitoring (basic, not fancy)

### Week 4 — Live canary
- $50 live test (not $1,000)
- Run 1 week, verify execution
- Scale to $200, then $500, then $1,000 over 4 more weeks

### Phase 2+ (after live validation)
- Add second strategy (PAXG pairs, funding mean reversion, etc.)
- Add regime filter
- Add ML only after 100+ own trades

---

## 7. Conventions for AI editors

### File naming
- Python: `snake_case.py`
- Config: `snake_case.yaml`
- Notebooks: `YYYY-MM-DD-topic.ipynb`
- Docs: `YYYY-MM-DD-TOPIC.md`

### Code style
- **Type hints on all functions**
- **Docstrings on all public functions** (Google style)
- **`if __name__ == "__main__":`** at bottom of runnable scripts
- **No print statements in production code** — use the `logging` module
- **No secrets in code** — always `.env` and `python-dotenv`

### Tests
- **`pytest` in `tests/`** mirroring `src/` structure
- **Test risk module first** — it's the safety layer
- **Mock the SDK** in tests, never hit real APIs in unit tests

### When you don't know
- **Ask Slim.** Don't guess on strategy parameters, risk limits, or scope.
- **Don't propose adding features not in scope.** The Out of Scope list is sacred until Slim changes it.

### When you find a bug
- **Fix it.** Don't paper over with a comment.
- **Add a test** that reproduces the bug before the fix.

### When you change something
- **Update the changelog** at `changelog.md` (project root) — most recent first.
- **Update the strategy log** at `vault/strategy-log/<strategy>.md` if it's a strategy change.
- **Update this AGENTS.md** if the change affects scope, stack, or rules.

---

## 8. Vault (second brain) pointers

The Obsidian vault at `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\` is the project's memory. **Code lives in the repo, knowledge lives in the vault.** Update both.

Key files:
- `gold-silver-hyperliquid.md` — vault AI context map (mirror of this file, more detail)
- `notes/2026-08-01-PIVOT-btc-eth-cascade.md` — pivot rationale (NEW, 2026-08-01)
- `notes/2026-08-01-DECISION-gs-strategy-build-path.md` — original decision (DEPRECATED, kept for history)
- `changelog.md` — milestone log
- `strategy-log/_index.md` — per-strategy performance tracking
- `journal/_index.md` — live trade journal template
- `strategies/_index.md` — strategy deep-dives
- `research/_index.md` — raw research

The vault is **Obsidian-flavored** with `[[wikilinks]]` and YAML frontmatter. Match the style.

---

## 9. Quick reference

| Thing | Value |
|---|---|
| Project name | HyphyLiquid (name kept despite pivot — "hyphy" = energy, still fits) |
| Bankroll | $1,000 USDC |
| Strategy | BTC/ETH liquidation cascade counter-trade |
| Venue | Hyperliquid mainnet |
| Reference bot | WickHunter (31★) |
| Risk/trade | 0.5-1% = $5-$10 |
| Leverage cap | 10x |
| First live trade | Target: ~4 weeks from 2026-08-01 |
| Capital ramp | $50 → $200 → $500 → $1,000 |
| Honest monthly target | 12-15% (not 25-33%) |
| Honest WR target | 55-65% |
| Honest PF target | 2.0+ |
| GitHub description | "BTC/ETH liquidation cascade bot on Hyperliquid" (update from gold/silver) |

---

## 10. Last updated

2026-08-01 — Initial creation, post-recon pivot from gold/silver to BTC/ETH cascade. Scope locked.

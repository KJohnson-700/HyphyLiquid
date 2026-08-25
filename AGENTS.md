---
project: hyphyliquid
asset: crypto
strategy: btc-eth-sol-hype-cascade
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

We are building an automated **liquidation-aware derivatives-flow counter-trade bot** on **Hyperliquid mainnet** with a **$1,000 USDC bankroll**. Single project, single venue. The strategy class is liquidation cascade counter-trade; execution is asset-routed into two tracks. Conservative risk rules. SOL and HYPE were added 2026-08-02 (HYPE is HL-native, SOL has 20x lev and the deepest liquidity after BTC/ETH). DOGE and BNB were added 2026-08-02 as **research-only** data sources.

**Symbol split (hard guard):**

- `v1_trade_symbols = BTC, ETH, HYPE` — active execution, OrderManager trades these (HYPE promoted 2026-08-24)
- `research_symbols = SOL, DOGE, BNB, xyz:GOLD, xyz:SILVER, xyz:* HIP-3 names` — passive data collection only, no orders

The split is enforced at the `OrderManager.execute()` level (refuses non-v1 symbols with `rejected_v1_allowlist`) and surfaced in `scripts/status.py`. Backtests always report per-symbol, never blended across the two groups.

**This is NOT a gold/silver project.** That was the original framing and recon (Week 0) proved it unviable — PAXG is the only metal perp on HL and it's too thin to support a $500+ strategy. We pivoted to crypto on 2026-08-01. See `vault/notes/2026-08-01-PIVOT-btc-eth-cascade.md` for the full pivot rationale (vault path: `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\notes\`).

---

## 1. Scope (what we ARE building)

### Strategy

> **Updated 2026-08-25.** The lane that is actually built, running hourly, and
> scored against the graduation ladder is **`funding_neg_fade`**
> (`scripts/paper_funding_neg_fade.py`): go long when funding is sufficiently
> negative, on the thesis that negative funding pays longs to hold, so fading
> it collects carry rather than betting direction. See
> `docs/THESIS-funding-neg-fade.md`. The cascade tracks below remain the
> longer-term plan and share the same primitives, but no cascade lane currently
> has a scorecard.

**Liquidation cascade counter-trade, asset-routed into two execution tracks.** Both tracks share the same trigger (liquidation burst from public trades) and the same primitives (event VWAP, OI delta, funding, BBO spread, L2 imbalance, ATR). They differ in the response classifier and time horizon:

- **BTC/ETH track — `liquidation_fade_or_follow`.** Detect burst, measure immediate book response, wait 1-5 min. Reclaim/absorption → fade. Failed reclaim + continued pressure → follow. Unclear → no trade. Built for the deeper, more directional BTC/ETH books where cascades can be either exhaustion or continuation. **This is the v1 build path** (the original BTC/ETH scope, expanded). Spec: `docs/2026-08-02-RESEARCH-btc-eth-hyperliquid-strategy-sweep.md`.
- **SOL/HYPE track — `range_sweep_liquidation_scalp`.** Detect range-compression setup, wait for band extreme + liquidation burst, require reclaim/wick-rejection confirmation, fade back to mid-band, max hold 5-30 min. Built for the choppier alts where bands actually compress. **Phase 2** — gated on BTC/ETH getting 30+ live trades first (honors "validate, then add" below).

Reference: WickHunter (31★) for the original counter-trade pattern. Hybrid doc: `docs/2026-08-02-RESEARCH-hybrid-liquidation-strategies.md`.

### Venue
**Hyperliquid mainnet** (testnet first for auth proof, mainnet for live). Official Python SDK. Wallet-based auth, no KYC.

### Capital
- **Total bankroll:** $1,000 USDC
- **Risk per trade:** 0.5-1% = $5-$10
- **Leverage cap:** 10x (HL allows up to 40x on BTC, 25x on ETH, 20x on SOL, 10x on HYPE; we cap ourselves at 10x across the board)
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
- ❌ **Second strategy in parallel from day 1** — single strategy, validate, then add. Don't diversify before we have data. The two execution tracks above are *not* a violation: they are the same liquidation-cascade strategy with asset-routed response classifiers. A genuinely new strategy (e.g. trend-following, funding arb) is still blocked until the v1 track is validated.
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
│   │   └── cascade.py      ← BTC/ETH/SOL/HYPE liquidation cascade strategy (with asset-routed response classifiers)
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
- **Agent (API) wallet for the bot** — master wallet never touches the running process. Approve the API wallet once in the HL UI. The API wallet can trade; it cannot withdraw.
- **Bracket entry** — every entry has an atomic TP and SL via the SDK bracket primitive, both reduce-only. Use `normalTpSl` for new positions, `positionTpSl` for existing.
- **128-bit hex client order IDs** prefixed `0x`, recorded in the journal for every order.
- **Funding rate P&L** tracked hourly (Hyperliquid settles funding every hour, not every 8h as on most CEXes)
- **Decision recorder** writes every signal decision to `data/decisions_*.jsonl` from week 2 onward, enabling deterministic replay
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

### Data integrity (HARD RULE — added 2026-08-25 after a look-ahead bug)
- **Never derive funding from `asset_ctx` snapshots.** `asset_ctx.funding` is the
  rate for the *upcoming* settlement. Stamping it with the poll hour puts each
  hour's funding on the previous bar, so the strategy trades on a rate the venue
  has not published — look-ahead that inflates every result.
- ✅ Build panels with `scripts/build_funding_from_venue.py` and
  `scripts/build_candles_from_venue.py` (HL `fundingHistory` / `candleSnapshot`;
  authoritative and re-fetchable, so local retention is not on the critical path).
- ❌ **Do not put `build_funding_panel.py` or `build_panels_from_duckdb.py` in any
  loop.** They are snapshot-derived. They silently re-corrupted the panel hourly
  after the first fix.
- 🔍 Run `scripts/panel_health.py` before trusting any result. It checks coverage
  *and* venue alignment, and is a blocking step in `fade_paper_daemon.py`.
- **A step exiting 0 is not progress.** Check the metric the loop exists to move.
- **Verify against the venue, not against our own capture.**

### When you change something
- **Update the changelog** at `changelog.md` (project root) — most recent first.
- **Update the strategy log** at `vault/strategy-log/<strategy>.md` if it's a strategy change.
- **Update this AGENTS.md** if the change affects scope, stack, or rules.

---

## 8. Vault (second brain) pointers

The Obsidian vault on this Mac is `~/Documents/Hermes Second Brain/projects/hyphy-liquid-bot/` (the `C:\Users\AbuBa\...\gold-silver-hyperliquid\` path below is the retired Windows location). **Only HyphyLiquid files may be edited — PSB-1 and Oracle-3 notes in that vault are off-limits.** The project's memory lives there. **Code lives in the repo, knowledge lives in the vault.** Update both.

Key files:
- `gold-silver-hyperliquid.md` — vault AI context map (mirror of this file, more detail). Load order: Pillar A strategic → B strategy → C execution.
- `notes/2026-08-01-PIVOT-btc-eth-cascade.md` — pivot rationale (active, 2026-08-01).
- `notes/2026-08-01-DECISION-gs-strategy-build-path.md` — original decision (DEPRECATED, kept for history).
- `changelog.md` — milestone log.
- `research/_index.md` — raw research. Active:
  - `research/2026-08-01-HYPERLIQUID-BTC-ETH-LIQUIDATION-SWEEP.md` — public sweep.
  - `research/2026-08-01-AI-MCP-CODEX-SWEEP-btc-eth-liquidation.md` — AI/MCP/Codex sweep.
- `strategies/_index.md` — strategy deep-dives. Active:
  - `strategies/liquidation-cascade.md` — v1, the only strategy in scope.
- `strategy-log/_index.md` — per-strategy performance tracking template. Active:
  - `strategy-log/liquidation-cascade.md` — pre-live stub.
- `journal/_index.md` — live trade journal template. Active:
  - `journal/2026-08-01-trades.md` — pre-live stub.

The vault is **Obsidian-flavored** with `[[wikilinks]]` and YAML frontmatter. Match the style. When adding a new file in any folder, also update that folder's `_index.md` table.

---

## 9. Quick reference

| Thing | Value |
|---|---|
| Project name | HyphyLiquid (name kept despite pivot — "hyphy" = energy, still fits) |
| Bankroll | $1,000 USDC |
| v1 trade symbols | BTC, ETH, HYPE (OrderManager refuses all others) |
| Research symbols | SOL, DOGE, BNB, xyz:* HIP-3 (data collection only) |
| Venue | Hyperliquid mainnet |
| Reference bot | WickHunter (31★) |
| Risk/trade | 0.5-1% = $5-$10 |
| Leverage cap | 10x |
| First live trade | Not scheduled — no lane clears Gate 1 (2026-08-25) |
| Capital ramp | $50 → $200 → $500 → $1,000 |
| Honest monthly target | 12-15% (not 25-33%) |
| Honest WR target | 55-65% |
| Honest PF target | 2.0+ (gate is PF >= 1.5; see `src/strategy/graduation.py`) |
| GitHub description | "BTC/ETH/SOL/HYPE liquidation cascade bot on Hyperliquid" (updated 2026-08-02 from BTC/ETH-only) |

---

## 10. Last updated

2026-08-01 — Initial creation, post-recon pivot from gold/silver to BTC/ETH cascade. Scope locked.

2026-08-02 — Scope expanded to BTC/ETH/SOL/HYPE. HyperPerps has heatmap data for BTC/ETH/SOL only (HYPE returns sample_size=0), so the HyperPerps-poller and paper-trade-loop pull 3 symbols; the Hyperliquid-direct daemons (WS collector, liquidation monitor, asset-ctx poller, candle fetch) pull all 4. Order manager fallback ticks/szDecimals extended for HYPE.

2026-08-02 (later) — Strategy split into two asset-routed execution tracks. BTC/ETH get `liquidation_fade_or_follow` (v1 build path, response classifier decides fade vs follow on event VWAP reclaim / failed reclaim within 1-5 min). SOL/HYPE get `range_sweep_liquidation_scalp` (Phase 2, gated on BTC/ETH getting 30+ live trades first). Both tracks share the same trigger (liquidation burst) and the same primitives (event VWAP, OI delta, funding, BBO spread, L2 imbalance, ATR). See `docs/2026-08-02-RESEARCH-btc-eth-hyperliquid-strategy-sweep.md` and `docs/2026-08-02-RESEARCH-hybrid-liquidation-strategies.md`.

2026-08-01 — AI/MCP sweep deltas: agent (API) wallet, bracket entry, 128-bit hex client order IDs, decision recorder (`data/decisions_*.jsonl`), WebSocket 4-channel pattern. No LLM in trade loop in v1. See `docs/2026-08-01-RESEARCH-REPOST-ai-mcp-codex.md`.

2026-08-24 — HYPE promoted into `V1_TRADE_SYMBOLS`; SOL price tick 0.001 added (both verified against the live L2 book). Graduation ladder implemented (`src/strategy/graduation.py`, `scripts/graduation_scorecard.py`) with persisted attestations in `data/attestations.json`. Position cap now enforced when scoring: the simulator held 4 lanes at once, `RiskConfig.max_open_positions` is 3, and replaying against it removed 21% of trades and 44% of net profit.

2026-08-25 — **Look-ahead funding bug found and fixed.** Panels were built from `asset_ctx` snapshots, which stamp each hour's funding on the previous bar; the simulator was trading a rate the venue had not published. Correlation against HL `fundingHistory` peaked at +1h on every symbol (mean 0.635 at 0h vs 0.946 at +1h). Panels now come from `fundingHistory` / `candleSnapshot`; alignment is 1.0000. Every result produced before this date was inflated — **no lane currently clears Gate 1** (HYPE PF 1.48, SOL 8.53 at n=11, ETH inf at n=10, BTC 1.47 at n=8). See §7 "Data integrity", `docs/CHANGELOG.md`, and `docs/THESIS-funding-neg-fade.md`. Testnet execution mode added (`--mode testnet_trading`), unarmed. WS collection trimmed to channels with a live consumer (~2.3 GB/day reclaimed).


# HyphyLiquid

Automated **BTC/ETH liquidation cascade counter-trade bot** on **Hyperliquid**.

> Built for the cascade edge — on-chain liquidations, OI divergence, and funding-rate extremes that no CEX exposes.

## Status

**Building** — see `AGENTS.md` for full scope and `changelog.md` for what's been done.

## Quick facts

- **Strategy:** Liquidation cascade counter-trade
- **Assets:** BTC, ETH
- **Venue:** Hyperliquid mainnet
- **Bankroll:** $1,000 USDC
- **Risk/trade:** 1% ($10)
- **Leverage cap:** 10x
- **Target:** 12-15% monthly (honest, not aspirational)
- **First live trade:** ~4 weeks from 2026-08-01

## Quick start

```bash
# Setup
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env  # then fill in wallet details
cp config/settings.example.yaml config/settings.yaml

# Verify
python scripts/market_recon.py  # recon what perps exist on HL

# Test (after Week 1)
pytest tests/

# Risk check (after Week 1)
python -c "from src.risk import RiskConfig, RiskManager; print(RiskManager(RiskConfig()).check_trade('BTC', 'long', 5000, 10, 8))"
```

## Repo layout

See `AGENTS.md §4` for the full tree. Short version:

```
src/risk.py           ← safety layer, built first
src/exchange/         ← HL SDK wrapper
src/strategy/cascade.py
src/execution/        ← order manager
src/journal/          ← trade journal
tests/                ← pytest, mirror src/
scripts/              ← utility scripts (recon lives here)
config/               ← settings.example.yaml
```

## For AI editors

**Read `AGENTS.md` first.** It defines scope, stack, conventions, and the hard rules. Do not propose adding features that are listed in §2 "Out of Scope."

## Vault (second brain)

Knowledge lives in Obsidian at `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\`. The pivot decision and historical context are there.

## License

Private / unreleased.

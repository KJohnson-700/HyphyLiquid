# HyphyLiquid Research Repost — AI / MCP / Codex Sweep

**Date:** 2026-08-01
**Scope:** Public AI-assisted Hyperliquid repos, Claude Code + Codex MCP/skills, execution-layer patterns.
**For:** AI editors (Claude Code, Cursor, OpenCode, Codex, Copilot, Aider).
**Companion vault note:** `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\research\2026-08-01-AI-MCP-CODEX-SWEEP-btc-eth-liquidation.md`
**Companion Round 1 repost:** `docs/2026-08-01-RESEARCH-REPOST-btc-eth-liquidation.md`
**Strategy deep-dive:** `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\strategies\liquidation-cascade.md`
**Backlinks in vault:** `strategies/liquidation-cascade` · `strategy-log/liquidation-cascade` · `journal/2026-08-01-trades`

## One-line conclusion
The AI/MCP layer around Hyperliquid is mature (108+ MCP repos at sweep time) but is overwhelmingly infrastructure — MCP tools, skills, agent wallets, execution wrappers. No public repo shows audited live alpha from fading BTC/ETH liquidations. Several execution patterns are worth copying; no strategy from the public corpus should be imported wholesale.

## Claude Code Hyperliquid MCP servers (verified from primary repos)

| Repo | Surface | Use |
|---|---|---|
| https://github.com/mektigboy/server-hyperliquid | TS, read-only MCP: mids, candles, L2 | Read-only wiring reference |
| https://github.com/kukapay/hyperliquid-info-mcp | Python, account/orders/fills/funding + candles | Strongest read-side schema |
| https://github.com/kukapay/hyperliquid-whalealert-mcp | CoinGlass whale alerts >$1M | Whale context feature |
| https://github.com/edkdev/hyperliquid-mcp | PyPI `mcp-hyperliquid`, full trading + bracket orders | **Closest execution model** |
| https://github.com/caiovicentino/hyperliquid-mcp-server | 27 tools incl. 4 WebSocket channels | WebSocket pattern |
| https://github.com/GigabrainGG/hyperliquid-mcp | FastMCP, OCO grouping logic | OCO decision reference |
| https://github.com/Impa-Ventures/hyperliquid-mcp | TS using nomeida SDK | TS wiring only |
| https://github.com/ChainGPT-org/chaingpt-claude-skill | 154 tools, custody-free, prompt-injection-resistant policy gate | Architecture reference for fail-closed policy |

Install mechanics for Claude Code:
- `claude mcp add <name> -s user -t stdio -e KEY=VAL -- npx -y <package>` for stdio servers.
- Skills: copy `SKILL.md` into `.claude/skills/<name>/SKILL.md` or `~/.claude/skills/<name>/`.
- `/install-skill <owner/repo>` from inside Claude Code, or via marketplace flow for plugins.
- Plugins shipping MCP servers need a full Claude Code restart, not `/reload-plugins`.

## Codex Hyperliquid integrations (verified)

There is **no first-party ChatGPT Codex Hyperliquid plugin.** Codex uses the same MCP stdio and skill mechanisms:

| Repo / Skill | Path | Notes |
|---|---|---|
| https://github.com/0xArchiveIO/0xarchive-mcp | `npx -y @0xarchive/mcp-server` stdio | 111 typed tools incl. `get_liquidations`, `get_liquidation_volume` |
| https://github.com/0xArchiveIO/0xarchive-skill | `.agents/skills/0xarchive/SKILL.md` | Native Codex skill install path |
| https://github.com/hypurrquant/perp-cli | `npx skills add hypurrquant/perp-cli` | Multi-agent install for Claude Code + Cursor + Codex + Gemini CLI |
| https://github.com/renlulu/trade-alt-skills | `~/.codex/skills/hl-leaderboard/` and `~/.codex/skills/hl-trade/` | Codex-native skills, agent-wallet auth |
| https://github.com/aicoincom/coinos-skills | per skill install | Works across Claude Code / Cursor / Codex / OpenClaw / Hermes / Windsurf / Gemini CLI |

Codex-specific findings:
- Codex reads skills from `.agents/skills/<name>/SKILL.md` or `~/.codex/skills/<name>/SKILL.md`.
- The same MCP stdio command that works in Claude Code works in Codex with MCP enabled.
- `npx skills add <owner/repo>` is the vendor-agnostic installer.
- **No Codex-specific liquidation strategy was found.**

## Execution-layer patterns we will adopt

1. **Agent (API) wallet** for the bot. Master wallet stays in Trezor/MetaMask; the API wallet can trade, cannot withdraw. Approval once in the HL UI. Pattern from `renlulu/trade-alt-skills` and `edkdev/hyperliquid-mcp`.
2. **Bracket order on every entry** — atomic entry + TP + SL, both reduce-only, via the SDK's bracket primitive. Pattern from `edkdev` and `GigabrainGG`.
3. **OCO grouping** — `normalTpSl` for new positions; `positionTpSl` for existing. Enforce exactly one grouping per order call.
4. **128-bit hex client order IDs** prefixed `0x`, recorded in our journal so order state is reconstructable from the journal without the exchange.
5. **Order type cheat sheet:**
   - Limit GTC: `{"limit": {"tif": "Gtc"}}`
   - Market IOC: `price: "0"` + `{"limit": {"tif": "Ioc"}}`
   - Trigger: `{"trigger": {"trigger_px", "is_market", "tpsl"}}` where `tpsl` is `"tp"` or `"sl"`
6. **WebSocket layer** with 4 channels: user events, market data, order updates, active subscriptions list. Reconnect-with-gap; record event timestamps.
7. **Liquidation dataset bootstrap** via `0xArchiveIO/0xarchive-mcp` — pull 6–12 months before coding the detector.
8. **Decision recorder** writing JSONL from week 2 onward (`data/decisions_*.jsonl`) for deterministic replay.
9. **Agent-safe CLI envelope** for any internal tool that an LLM may touch: `{ok, data, meta, retryable}`. Not in the hot path in v1, but reserve the contract.
10. **Plugin-based risk** — protections (max_drawdown, daily_loss, consecutive_loss, position_timeout) as togglable modules, per-symbol locks. Pattern from `web3spreads/quant-flow`.
11. **Trade ledger** with reasoning + confidence + optional hash for the Phase 2+ journal.

## Strategy-specific learnings (carry to cascade detector)

These are observed patterns, not validated alpha. Each needs to be backtested locally.

- **Group adjacent liquidations** within a 30–120 s window into one cascade. Allow only one entry per cascade.
- **Minimum event notional gate** sized to keep expected price impact < 10 bps on inside 20 levels.
- **Pre-place IOC limit at pre-event mid + slippage budget** (start 5 bps BTC, 8 bps ETH); escalate to sweep if unfilled in 2 s. Do **not** auto-market on alert.
- **Funding as context filter**, not trigger. Fade only when funding has reverted toward zero or against us.
- **Bracket both legs** with TP = pre-event mid and SL = pre-event low − buffer; mirror for short liquidations.
- **5-minute cooldown** after a loss in the same market; combine with the 3-loss circuit breaker in `AGENTS.md`.
- **Skip HIP-3 / HIP-4** in v1. HIP-4 is binary probability, HIP-3 has lower OI.

## Decision deltas vs Round 1 (REPOST v1)

### Keep (from Round 1, unchanged)
BTC/ETH only. Hyperliquid only. 10x leverage cap. 1% risk/trade. 3 positions. Daily 3% / weekly 5% breakers. Three-loss halt. Bankroll <$600 → stop. Reduce-only exits. Testnet → paper → $50 canary → staged ramp.

### Add (from this sweep)
- Use **agent (API) wallet** for the bot — never the master wallet.
- **Bracket on every entry.** Reduce-only TP and SL.
- **128-bit hex client order IDs**, journaled.
- **OCO grouping** rules (`normalTpSl` for new positions).
- **WebSocket** layer with the 4 channels above, reconnect-with-gap.
- **Liquidations dataset** from `0xArchiveIO/0xarchive-mcp` before coding the detector.
- **Decision recorder** in `data/decisions_*.jsonl` from week 2.
- **Plugin-based protections** (drawdown, daily loss, consecutive loss, position timeout) as togglable modules.
- **ATR- or pre-event-level based TP/SL**, not fixed percent.
- **No LLM in the trade loop** in v1. AI is for research, post-mortem, and skill wiring only.

### Do not add in v1
- LLM-driven trade decisions (FinCoT, bull/bear debate, regime-adaptive prompts).
- Multi-agent orchestrators or self-evolving strategies.
- TradingView webhook ingestion (third party we do not need yet).
- PAXG, HIP-3, HIP-4, other venues, second strategy.
- Hyperliquid 25x / 50x on BTC/ETH.

## What the AI layer does NOT solve
- No public code shows audited, risk-adjusted, live alpha from fading liquidations.
- Most "AI trading bot" repos are wrappers, dashboards, or alert systems.
- LLMs add latency the cascade edge does not tolerate. Sub-second decisions cannot use LLM reasoning on the hot path.

## Confidence
- **High:** MCP/skill install mechanics; agent-wallet pattern; bracket/OCO API; agent-safe envelope; liquidation dataset via 0xArchive.
- **Medium:** execution tips improve robustness but are not independently validated for our market/size.
- **Low:** any claim of live PnL from these repos. None publish a live equity curve.

## Open follow-ups
1. Pull 6–12 months of HL liquidations via `0xArchiveIO/0xarchive-mcp` and compute event-size distribution, post-event 1m/5m/15m price impact, and time-to-reversion by BTC/ETH.
2. Add agent-wallet setup step to `AGENTS.md §5` and to the testnet auth spike script.
3. Build the bracket wrapper in `src/execution/order_manager.py` using the SDK's primitives (not the MCP server) so we stay on the official SDK path.
4. Update `docs/2026-08-01-RESEARCH-REPOST-btc-eth-liquidation.md` with these deltas in a "Delta from AI/MCP Sweep" section.

## Sources
- https://github.com/search?q=hyperliquid+mcp&type=repositories
- https://github.com/search?q=hyperliquid+claude&type=repositories
- https://github.com/search?q=hyperliquid+codex&type=repositories
- https://github.com/search?q=hyperliquid+ai+trading+bot&type=repositories
- https://github.com/mektigboy/server-hyperliquid
- https://github.com/kukapay/hyperliquid-info-mcp
- https://github.com/kukapay/hyperliquid-whalealert-mcp
- https://github.com/edkdev/hyperliquid-mcp
- https://github.com/caiovicentino/hyperliquid-mcp-server
- https://github.com/GigabrainGG/hyperliquid-mcp
- https://github.com/6rz6/HYPERLIQUID-MCP-Server
- https://github.com/Impa-Ventures/hyperliquid-mcp
- https://github.com/ChainGPT-org/chaingpt-claude-skill
- https://github.com/0xArchiveIO/0xarchive-mcp
- https://github.com/0xArchiveIO/0xarchive-skill
- https://github.com/hypurrquant/perp-cli
- https://github.com/renlulu/trade-alt-skills
- https://github.com/aicoincom/coinos-skills
- https://github.com/claudefi/claudefi
- https://github.com/alsk1992/CloddsBot
- https://github.com/web3spreads/quant-flow
- https://github.com/KonScanner/hyperliquid-trader-tracker
- https://github.com/Drakkar-Software/OctoBot
- https://github.com/titouannwtt/freqtrade-ultimate
# HyphyLiquid — Mavis (Windows) handoff to Claude (Mac mini)

**From:** Mavis (root session on Windows) — Slim's primary AI agent
**To:** Claude (already running on the Mac mini per the last 30+ commits to main)
**Date:** 2026-08-26 23:40 PT
**Repo:** https://github.com/KJohnson-700/HyphyLiquid
**Author's note:** when I started this handoff, my local was at `dbea025` (the pre-Mac-mini state). The Mac mini Claude had been working concurrently and pushed 30+ commits to `origin/main` ahead of me — including a major reframe of the project (the cascade lane is dead, `funding_neg_fade` is v1, the panel was wrong, etc.). I rebased my HANDOFF.md on top of your commits and rewrote it. **Read this as a Windows-box shutdown report + cross-reference to your new state. Trust `docs/CHANGELOG.md` and `docs/LESSONS-AND-PLAYBOOK.md` over this file for any project-state question.**

> **Read order:** §0 (header) → §1 (what I just did on the Windows box) → §2 (state at handoff) → §3 (what I think the Mac mini has that Windows doesn't, and vice versa) → §4 (Mac mini setup if it's not already running) → §5 (cleanup).
> **Don't read `AGENTS.md` first for project state — it's still mostly the old 2026-08-01 framing.** Read `docs/CHANGELOG.md` (newest first) and `docs/LESSONS-AND-PLAYBOOK.md` instead. `AGENTS.md` needs a rewrite; that's on you.
> **The Obsidian vault at `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\` is NOT in this repo.** Vault is the second brain; the working mirrors are in `docs/`.

---

## 0. TL;DR

- The Windows box had **9 pythonw daemons** (5 logical, some running duplicate venvs) and **10 mavis crons** running. **All stopped and paused**, respectively.
- The Mac mini Claude has been working concurrently and is now the source of truth for the project state. **The v1 strategy is `funding_neg_fade`, not the BTC/ETH cascade.** ZEC has been promoted. The cascade lane is dead (PF 0.47 n=826).
- The Windows box has **historical data** (~316K liquidation events, ~9.5M trade prints, 35K paper decisions) in `C:\Users\AbuBa\Desktop\HyphyLiquid\data\`. Most of it is now reconstructible from the venue — see `docs/LESSONS-AND-PLAYBOOK.md` Part 5. **The `liquidations.jsonl` and per-day `paper_positions_*.jsonl` files are NOT reconstructible from the venue** — keep them.
- This HANDOFF.md was rebased on top of your `c587f0e` (last commit on remote at handoff). My commit `a09dd59` adds this file.

---

## 1. What I just did (the shutdown)

### 1.1 Daemons killed (9 pythonw processes)

| script | count | reason for duplicates |
|---|---|---|
| `scripts/poll_hyperperps.py` | 2 | one from `.codex_venv`, one from `codex-runtimes` cache |
| `scripts/collect_ws_data.py` | 2 | same venv split |
| `scripts/paper_trade_loop.py` | 2 | same venv split |
| `scripts/poll_asset_ctx.py` | 2 | same venv split |
| `scripts/liquidation_monitor.py` | 1 | Windows Store pythonw |

**Root cause of the duplicates:** the 8/22 daemons were launched with the system Python which has since lost `requests`/`websocket` packages, so 8/24 I switched to `.codex_venv\Scripts\pythonw.exe` — but the old ones from `codex-runtimes\dependencies\python\pythonw.exe` were never killed. **When you re-launch on the Mac mini, do not let this drift happen.** Either use a single launch wrapper for all daemons, or write a `launchd` plist that uses one venv.

**Note:** if your recent commit `c587f0e` (the notification webhook) and the changes to `scripts/collect_ws_data.py` and `scripts/paper_trade_loop.py` on the Mac mini changed the daemon set, the list above may be stale for you. The Mac mini daemons are the truth.

### 1.2 Crons paused (10 mavis crons, all set to `enabled: false`, status `paused`)

| cron name | was schedule (PT unless noted) | what it does | cron ID |
|---|---|---|---|
| `panel_refresh` | `*/5 * * * *` | candle + funding panel rebuild | `535d1bbf-5bc1-498e-9ba2-ae66c37621f8` |
| `duckdb_ingest` | `5,20,35,50 * * * *` | JSONL → DuckDB, `--incremental` | `a6b3820f-f57d-4963-afa4-e732335ea289` |
| `binance_funding_fetch` | `* * * * *` UTC | cross-venue funding delta | `e3e0b36f-b588-4746-84bc-5f9007f665ea` |
| `paper_funding_neg_fade_live` | `5 * * * *` | funding-arb paper trader | `ff8a89cd-55aa-445d-ae52-46ae7fb0c6ab` |
| `paper_xyz_gold_bb_neutral` | `15 * * * *` | xyz:GOLD BB-neutral forward-paper | `c504c7c0-03e5-41c0-8fce-7f053f62ec65` |
| `daemon-health-check` | `0 * * * *` | 5/5 daemons + candle backtest gate | `3b59e7eb-48ca-4336-928c-ae5336afc20a` |
| `rebuild-cycle` | `5 12 * * *` daily | full sweep, `--skip-lanes` | `87a8a2b1-00e0-46c3-86f9-c674863a7efa` |
| `data_compress` | `00:55` daily | gzip old `.jsonl` | `8295b86d-9d45-4b3b-9753-ae6f8d185924` |
| `data_backup` | `01:05` daily | rclone → B2 (config still pending) | `c3626447-13fb-4195-88bd-191895e96798` |
| `vault-weekly-maintenance` | `Sun 9am` | vault pass | `f3022c38-7bf2-47eb-8b49-f31caab066b7` |

**⚠️ Important:** the cron prompts were written for Windows paths (`C:\Users\AbuBa\Desktop\HyphyLiquid\`, `python.exe`/`pythonw.exe`, etc.). If you re-enable any on the Mac mini, **rewrite the prompt body to use the Mac path and `python3` / `.venv/bin/python`**. Or better: re-create the crons fresh with Mac-native prompts.

**Also: many of these crons are now obsolete given your recent refactor.** The `panel_refresh` cron runs `build_candle_panel.py` + `build_funding_panel.py`, but `docs/CHANGELOG.md` says "❌ never in a loop: `build_funding_panel.py`, `build_panels_from_duckdb.py`". The cron is now actively harmful. Disable it permanently; use `panel_health.py` as a blocking step in the daemon instead.

### 1.3 Other cleanup
- Cleared stale lock files (`.rebuild_cycle.lock`, cron pid files) — Windows box is clean
- Wrote this `HANDOFF.md` and rebased it onto `c587f0e` (your latest commit)
- **Did NOT** clean the root-level log/pid/cron debris (`.cron_*.log`, `.run_*.ps1`, etc.) — these are on the Windows box only and will be discarded when the Windows repo is archived. See §5.

---

## 2. State of the Windows box at handoff

### 2.1 Paper audit (most recent: 2026-08-23 21:42 PT, `data/paper_audit_latest.md`)

⚠️ **All numbers in this section are STALE.** They were captured on the Windows box using the OLD panel builders (with look-ahead bias, stale sources, etc.). The Mac mini has the corrected numbers in `docs/CHANGELOG.md`. I'm keeping them here only for context.

| metric | value (Windows side, 8/23) | value (Mac mini side, 8/26) |
|---|---|---|
| Total decisions | 35,950 | (see `docs/CHANGELOG.md`) |
| Opened / Closed / Open | 164 / 164 / 0 | (see scorecard) |
| Net PnL | -$705.05 | (recalibrated) |
| PF | 0.5924 | (recalibrated) |
| Win rate | 35.98% | (recalibrated) |
| HYPE trades | 8, PF 0.0, WR 0% (all stopped) | **n=88, PF 1.71** ← the corrected number |

The corrected story is in `docs/CHANGELOG.md` and `docs/LESSONS-AND-PLAYBOOK.md`. The HYPE "PF 2.52 on 17 trades" I had was **substantially the look-ahead bug** — corrected number is **PF 1.71 on 88 trades**, which still passes the gate but is a real number, not a phantom.

### 2.2 Testnet proof (still valid)
`data/testnet_proof_status.md`: `bracket_proof_passed`, protective stop visible, flat cleanup. Generated 2026-08-22.

### 2.3 Live-like readiness (last: 2026-08-21 22:53 PT)
`data/live_like_readiness_status.md`: `HOLD` — "exchange snapshot not fetched; ok for paper, not enough for live". The `daemon-health-check` cron would have re-evaluated this hourly but was paused.

### 2.4 xyz:GOLD forward-paper (still running as of last cron tick)
`data/paper_xyz_gold_bb_heartbeat.txt`: n=0 closed, **1 open position** (long @ $4641.20, entry 2026-08-26 04:00 PT, trail_sl $4604.07, gate=HEURISTIC). The position is in paper state; when the Mac mini picks up, it will need to decide what to do with it.

⚠️ **Per your `docs/CHANGELOG.md`:** the RWA lanes (xyz:*) are all in the "What is dead" list (cut, n=26-103, net PF 0.31-1.72). So the entire xyz:GOLD forward-paper lane is probably dead per your reframe. The cron `paper_xyz_gold_bb_neutral` is also paused, so no new ticks will fire. **This open paper position is now orphan data — close it out by hand (set exit to current price or let it time out) when you decide.**

### 2.5 Other state files (Windows box, last values)

| file | last value | meaning |
|---|---|---|
| `data/.rebuild_baseline.json` | 316,656 liquidations, last event 2026-08-23 21:38:53 UTC | Liquidation count when last cycle ran. Stale — 3 days without a cycle (daemons killed). |
| `data/event_features.jsonl` | last write before daemon kill | Per-event L2/asset_ctx snapshot. Same scope as the Mac mini's `data/ws_*/` — may be redundant now. |
| `data/whale_features_latest.json` | 2026-08-18 08:03 UTC (old) | Pre-Mac-mini shadow context. Superseded by the new whale pipeline on the Mac. |
| `data/paper_xyz_gold_bb_state.json` | has open position | See §2.4. |
| `data/hyphyliquid.duckdb` | 651MB, 5 tables, last full reload 8/23 | Per `docs/CHANGELOG.md`, panels now come from venue; DuckDB may be obsolete on the Mac. |
| `data/paper_decisions_20260822.jsonl` | 164 lines | Last day's paper decisions. Has the random-id bug per the playbook — duplicate detection will be needed. |

---

## 3. What the Mac mini has that the Windows box doesn't (and vice versa)

### 3.1 Mac mini (Claude) has (newer):
- **`docs/CHANGELOG.md`** — the authoritative new changelog with all the reframe
- **`docs/LESSONS-AND-PLAYBOOK.md`** — the measurement traps and how to use the tooling (read this before trusting any number)
- **`docs/THESIS-funding-neg-fade.md`** (implied) — the actual v1 strategy thesis
- **`scripts/build_funding_from_venue.py` + `scripts/build_candles_from_venue.py`** — venue-based panel builders (the local jsonl builders are dead, never use in a loop)
- **`scripts/panel_health.py`** — blocking health check
- **`scripts/graduation_scorecard.py`** — where every lane stands
- **New strategies**: `funding_neg_fade`, `swing lane` (ZEC), `range_sweep`, etc. — all on venue-aligned data
- **Free venue data**: leaderboard, positions, fill history, resting orders — `analyze_whale_fills.py`, `whale_strategy_patterns.py`
- **Notification webhook** with `User-Agent` pinned, refuses to break trading (every call site swallows exceptions) — commit `c587f0e`
- **ZEC promoted** to v1 allowlist (n=109, PF 1.70, swing lane)
- **HYPE n=88, PF 1.71** — recalibrated, clears every gate except `decision_path_has_tests`
- **Testnet execution ARMED** (needs `--arm-testnet` to actually place orders)
- **BTC, SOL removed** from PER_ASSET_POLICY (both fail in both halves)
- **All 30+ commits** of reframe work

### 3.2 Windows box (Mavis) had (older, possibly useful for history):
- **`data/liquidations.jsonl`** — 316K events from the 5-second-scan daemon. Not reconstructible from venue (HL doesn't publish historical liquidation prints the same way — HyperPerps is the only historical source and it has DNS issues per the 8/3 changelog).
- **`data/trades/`** — 9.5M trade prints in per-day jsonl files. Most are now reconstructible from the venue (HL candle/trade REST endpoints), but the per-trade prints are useful for backtesters that want more granularity than candles.
- **`data/paper_positions_*.jsonl`** — 19 days of paper decisions, 164 opens/closes, the entire history. **Has the random-id bug** (see `docs/LESSONS-AND-PLAYBOOK.md` 2.5) — 66 rows hold 57 real trades. Useful for paper→testnet→mainnet comparison if you back-fill the corrected `paper_id`.
- **`data/asset_ctx/`, `data/hyperperps_snapshots/`, `data/ws_*/`** — historical context that's now mostly redundant (venue endpoints cover it). Keep for archival only.
- **`logs/cycle_*.log`, `logs/poller_*.log`, etc.** — operational logs from the 8/22-8/26 window. Useful for postmortems.
- **The Obsidian vault** — second brain with the design history, decisions, daily notes. Not in the repo. Slim decides how to migrate it to the Mac mini.

### 3.3 What I would NOT do
- Don't `rsync` the Windows `data/` over to the Mac and then have two data lakes that drift. Pick one.
- Don't try to "merge" the Windows-side paper history with the Mac mini's corrected one. The Windows paper ledger has the random-id bug, the wrong panel, the level-vs-edge bug, etc. Treat it as **a historical record of what the OLD code produced**, not as data to keep trading on. If you want continuity, re-run the fade lane from a clean slate and start a new paper ledger.
- Don't keep the `data/hyphyliquid.duckdb` — it's 651MB and was built from the old panel. Rebuild it from the venue if you need it.

---

## 4. Mac mini setup notes (if you haven't already done it)

The Mac mini was already committing to `origin/main` as of 2026-08-26 21:45 PT (your commit `c587f0e`). So presumably it's set up and running. This section is for sanity-checking the state.

### 4.1 Things to verify
```bash
cd ~/path/to/HyphyLiquid
git status                        # should be clean
git log --oneline origin/main -5  # should show c587f0e at head
python3 -m venv .venv             # if not already there
source .venv/bin/activate
pip install -r requirements.txt   # if not already installed
pytest -q                         # should be ~824 passing per c587f0e
```

### 4.2 Daemon launch on macOS
- `pythonw` doesn't exist on Mac. Use `nohup .venv/bin/python scripts/X.py > logs/X.log 2>&1 &` or `tmux`/`screen`/ `launchd`.
- The 5 daemons (`poll_hyperperps.py`, `collect_ws_data.py`, `paper_trade_loop.py`, `liquidation_monitor.py`, `poll_asset_ctx.py`) may or may not still be needed — your recent work has refactored much of this. Check `scripts/` for the new daemon set (likely `fade`, `whale`, etc. per the playbook Part 5).

### 4.3 Cron re-enable
- If you want the same mavis crons, re-create them with Mac-native prompts (paths, `python3` instead of `python.exe`).
- **Do NOT re-enable `panel_refresh`** — it uses the deprecated `build_funding_panel.py`. Use `panel_health.py` as a blocking step in the daemon instead.
- The other 9 crons: re-evaluate whether they're still needed given the new architecture. The `duckdb_ingest` cron may be obsolete (DuckDB is no longer on the critical path). The `binance_funding_fetch` is irrelevant now that panels come from the venue.

### 4.4 Data carryover
- **Don't sync `data/` from Windows unless you have a specific reason.** The venue-sourced panels are the new truth. The Windows `data/` is mostly historical archival.
- **Do sync `logs/` if you want postmortem material** — but tag it `windows-side-2026-08-22-26/` so it's clearly separate from the new Mac-mini logs.

---

## 5. Cleanup (post-handoff)

> **These are for the Mac mini Claude to do once the Windows box is no longer needed.** Slim's instruction was "clean the repo of the extra info after everything is transferred" — meaning once the Mac mini is running clean and verified, prune what's left behind.

### 5.1 On the Windows box (when Mac mini is verified)
- **Archive `C:\Users\AbuBa\Desktop\HyphyLiquid\data\`** to a Backblaze B2 bucket or external drive before deleting (or set up the B2 backup cron that was failing on the Windows box). The `liquidations.jsonl` and `paper_positions_*.jsonl` are the irreplaceable bits.
- **Delete the rest of `C:\Users\AbuBa\Desktop\HyphyLiquid\`** after archive. The repo on the Mac mini is the source of truth.
- **Keep the Obsidian vault** at `C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid\` — Slim's call on how to sync it. Options: Obsidian Sync (paid), iCloud, Dropbox, git, or just USB.
- **Don't delete `.env`** — it has the HL testnet keys. The user should move it to the Mac mini's `.env` (or re-create it).

### 5.2 In the repo (for Mac mini Claude to commit)
- **Add to `.gitignore`**: the root-level debris that was on the Windows box (not on the Mac mini, but defensive):
  ```
  /0
  /.cron_*
  /.run_cron_*
  /.run_candle.*
  /.tmp_*
  /cf_err.txt /cf_out.txt
  /fetch_err.txt /fetch_out.txt
  /hl_funding_*.txt
  /build_candle.* /build_funding.*
  /candle_*.log* /funding_*.log*
  /cf_err.log /cf_out.log
  ```
- **Rewrite `AGENTS.md`** — it's still mostly the 8/1 framing. The v1 strategy section should now lead with `funding_neg_fade`, not the cascade tracks. The cascade tracks should be moved to "Phase 2 (longer-term, no scorecard yet)" with an honest "no cascade lane currently has a scorecard" disclaimer. Add a "Read `docs/CHANGELOG.md` first" warning at the top.
- **Update `changelog.md`** (the old one at the repo root) — it's still the 8/24 hot-cron-prune entry. Either delete it or merge with `docs/CHANGELOG.md`. They're the same thing now, just two files.
- **Add a CI check** that fails if non-source files appear at the repo root (defensive against future cron/debug drift).
- **Document the launch path** — write `logs/START_DAEMONS.md` with the canonical venv path, the launch command, and the 5 (or however many) daemons. This was a 2026-08-24 recommendation that was never done.

### 5.3 Things I noticed but didn't fix
- The Windows-side `data/.rebuild_baseline.json` still has `liquidation_count: 316656` from 3 days ago. The Mac mini Claude's equivalent will be different (you have venue-aligned liquidations, not the 5s-scan-derived ones). Just be aware.
- The `data/.liquidation_monitor_state.json` is Windows-specific and uses Windows paths. Won't apply to the Mac mini.
- There are 19 `.rebuild_cycle.lock.*` historical files in `data/` from the cron drift. The Mac mini's lock handling may need similar cleanup eventually.

---

## 6. Quick answers to the questions Slim might ask

**Q: What's the current state of the project?**
A: Per `docs/CHANGELOG.md` and `docs/LESSONS-AND-PLAYBOOK.md` (Mac mini Claude, 8/26): the v1 strategy is `funding_neg_fade`, HYPE clears every gate except `decision_path_has_tests` (which clears on the first testnet fill), ZEC is in v1, BTC and SOL are out. Testnet execution is armed. The cascade lane is dead (PF 0.47 on n=826). Free venue data (leaderboard, positions, fills, resting orders) is the new moat.

**Q: Can we go live yet?**
A: Almost. HYPE is one testnet fill away from clearing the last gate. ETH needs `logic_makes_market_sense` attestation. ZEC needs a canary run. Mainnet would need the API wallet funded (~$50) and the `--arm-mainnet` flag flipped.

**Q: What's broken?**
A: Nothing the user can see. The Windows box is cleanly shut down. The Mac mini is presumably running. The 30+ commits to main are clean. The only weird state is the orphan xyz:GOLD paper position (§2.4) — needs a manual close.

**Q: Why is HANDOFF.md on the repo root?**
A: I wrote it before I knew Claude was already on the Mac mini. It was meant as a from-scratch handoff but turned into a Windows-side shutdown report + cross-reference. Keeping it because it documents the Windows box's state, which is useful for archaeology. Feel free to delete or move it to `docs/` after the next clean-up.

— Mavis (2026-08-26 23:40 PT, Windows box, root session)

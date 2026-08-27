# Starting the daemons

One canonical launch path. **Use `.venv/bin/python` and nothing else.**

The Windows box ended up running nine processes for five logical daemons —
two copies each of four scripts, from two different Python installs. The
2026-08-22 launch used a system Python that later lost `requests`/`websocket`,
so 08-24 relaunched from `.codex_venv` without killing the originals. Both sets
kept writing. That is the failure this file exists to prevent.

```bash
cd ~/Documents/HyphyLiquid
./scripts/mac_tmux_start.sh          # starts every daemon below, one venv
tmux ls                              # expect 10 sessions
```

`mac_tmux_start.sh` is the only supported launcher. Do not start a daemon by
hand unless you are debugging one, and if you do, kill the tmux session first
so two copies never run.

---

## The ten daemons

| session | script | what it does | consumer |
|---|---|---|---|
| `fade` | `fade_paper_daemon.py` | **the strategy loop** — venue panels → health gate → fade paper → swing paper → fade testnet → swing testnet → regime labels | the graduation scorecard |
| `whale` | `collect_whale_positions.py` | leaderboard cohort, positions, resting orders and stops | untested signal, collecting |
| `chain` | `collect_chain_metrics.py` | Robinhood Chain TVL, launch velocity, launchpads | untested signal, collecting |
| `pumpfun` | `collect_pumpfun.py` | pump.fun top 400: mcap, ATH, `reply_count` | untested signal, collecting |
| `listings` | `detect_new_listings.py` | Robinhood + Hyperliquid listing diffs, HL announcements | delisting warning |
| `ws` | `collect_ws_data.py` | trades → `data/trades`, l2Book, 1m candles | `liq`, event features |
| `liq` | `liquidation_monitor.py` | liquidation events + event features | research |
| `poller` | `poll_hyperperps.py` | HyperPerps heatmap snapshots | `paper` |
| `paper` | `paper_trade_loop.py` | cascade-lane paper decisions | research only — **lane is cut** |
| `ctx` | `poll_asset_ctx.py` | asset context snapshots | `binance_delta`, `build_cascades` |

Only `fade` trades. The rest collect.

---

## Verifying, not assuming

A daemon logging `ok` is not a daemon working. Two failures this week were
found by looking at a number, not a status line: an 11-hour stall where every
step printed `ok` while the closed-trade count never moved, and a lane that was
"armed" for four hours while every order it placed was refused.

```bash
python3 scripts/watch_trades.py      # positions, order attempts, distance to next signal
python3 scripts/panel_health.py      # MUST pass before trusting any number
tmux capture-pane -pt fade -S -50    # last 50 lines of the strategy loop
```

`watch_trades.py` shows **rejections as well as fills**. Every order this
project has attempted so far has been a rejection; a fills-only view would have
looked idle throughout.

Discord alerts fire on every fill, every rejection, and any aborted tick — so
the normal state is *not* watching a screen.

---

## Do not

- **Do not re-create the `panel_refresh` cron.** It ran `build_candle_panel.py`
  and `build_funding_panel.py` every 5 minutes. Those derive funding from
  polled snapshots, which stamps it an hour early — the look-ahead bug. It
  would silently re-corrupt the panel four times an hour. `panel_health.py`
  is a blocking step in the `fade` daemon and covers this properly.
- **Do not run a daemon from a second Python.** See the top of this file.
- **Do not add a collector without a named consumer.** Roughly 2.3 GB/day was
  being written to directories nothing read; `bbo`, `activeAssetCtx` and a
  duplicate raw trades copy were switched off on 2026-08-25.

---

## Stopping

```bash
tmux kill-session -t fade            # one
tmux kill-server                     # all
touch data/live_kill_switch.flag     # refuse new orders, leave collection running
```

The kill switch is checked by the testnet guard on every tick. Remove the file
to resume.

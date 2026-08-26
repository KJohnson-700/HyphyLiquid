#!/usr/bin/env bash
# mac_tmux_start.sh
#
# Idempotent tmux bootstrap for the five HyphyLiquid daemons on a Mac mini
# (or any Unix box). Safe to re-run; existing sessions with the same name
# are killed and replaced.
#
# Usage:
#   ./logs/mac_tmux_start.sh                 # uses cwd as REPO_DIR
#   ./logs/mac_tmux_start.sh /path/to/repo   # explicit REPO_DIR
#
# After it runs:
#   tmux ls                                  # see all 5 sessions
#   tmux attach -t ws                        # peek inside any one
#   tmux kill-session -t liq                 # stop one
#
# Logs land in REPO_DIR/logs/<name>.log (stdout + stderr merged).
#
# Verified on the Windows source:
#   - all five daemon scripts use pathlib.Path for paths (no hard-coded
#     Windows backslashes outside docstrings)
#   - requirements.txt now pins websocket-client for the WS collector
#   - HYPERLIQUID_ENV / HYPERLIQUID_PRIVATE_KEY / HYPERLIQUID_WALLET_ADDRESS
#     / HYPERLIQUID_BANKROLL come from .env (loaded via python-dotenv)
#
# IMPORTANT: do NOT copy the Windows .env to the Mac. Generate a fresh
# API wallet in the Hyperliquid UI, approve trading on it, and put the
# new key in REPO_DIR/.env. The master wallet should never be on the
# running box.

set -euo pipefail

# ---- config ------------------------------------------------------------------

REPO_DIR="${1:-$(pwd)}"
VENV_DIR="$REPO_DIR/.venv"
LOG_DIR="$REPO_DIR/logs"
PYTHON_BIN="$VENV_DIR/bin/python"

# Five daemons: name | script. Order matches the cron task in AGENTS.md.
DAEMONS=(
  "poller:poll_hyperperps.py"
  "paper:paper_trade_loop.py"
  "ws:collect_ws_data.py"
  "liq:liquidation_monitor.py"
  "ctx:poll_asset_ctx.py"
  # The five above only COLLECT data. This one runs the strategy that is
  # actually up for promotion, in paper mode — without it the fade lane
  # never accumulates trades and no asset can reach Gate 2.
  "fade:fade_paper_daemon.py"
  # Whale positioning: who is actually on which side, from the public
  # leaderboard + per-address positions. The only lane reading something other
  # than the same price/liquidation feed every other bot reads.
  "whale:collect_whale_positions.py"
  # New Robinhood / Hyperliquid listings. A labelled event history, not an
  # entry signal -- see the header of the script for why.
  "listings:detect_new_listings.py"
)

# ---- preflight ---------------------------------------------------------------

if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: REPO_DIR does not exist: $REPO_DIR" >&2
  exit 1
fi
if [[ ! -d "$REPO_DIR/scripts" ]]; then
  echo "ERROR: $REPO_DIR/scripts missing — clone the HyphyLiquid repo first" >&2
  exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo "ERROR: tmux not installed. On macOS: brew install tmux" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not on PATH" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

# ---- venv + deps -------------------------------------------------------------

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[venv] creating $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi
echo "[venv] upgrading pip"
"$PYTHON_BIN" -m pip install --upgrade pip --quiet

if [[ -f "$REPO_DIR/requirements.txt" ]]; then
  echo "[deps] installing requirements.txt"
  "$PYTHON_BIN" -m pip install -r "$REPO_DIR/requirements.txt" --quiet
else
  echo "WARNING: no requirements.txt in $REPO_DIR" >&2
fi

# ---- stop existing sessions with the same names ------------------------------

for entry in "${DAEMONS[@]}"; do
  name="${entry%%:*}"
  if tmux has-session -t "$name" 2>/dev/null; then
    echo "[tmux] killing existing session: $name"
    tmux kill-session -t "$name"
  fi
done

# ---- launch ------------------------------------------------------------------

cd "$REPO_DIR"
for entry in "${DAEMONS[@]}"; do
  name="${entry%%:*}"
  script="${entry##*:}"
  script_path="$REPO_DIR/scripts/$script"
  if [[ ! -f "$script_path" ]]; then
    echo "ERROR: missing $script_path" >&2
    exit 1
  fi
  echo "[tmux] starting: $name  ->  $script"
  # merged stdout+stderr to logs/<name>.log
  tmux new-session -d -s "$name" "$PYTHON_BIN $script_path 2>&1 | tee -a $LOG_DIR/${name}.log"
done

# ---- report ------------------------------------------------------------------

echo
echo "=== active HyphyLiquid tmux sessions ==="
tmux ls
echo
echo "=== one-line status (first 3 lines of each log) ==="
for entry in "${DAEMONS[@]}"; do
  name="${entry%%:*}"
  log="$LOG_DIR/${name}.log"
  if [[ -s "$log" ]]; then
    echo "--- $name ($log) ---"
    head -3 "$log" | sed 's/^/  /'
  else
    echo "--- $name : log empty (daemon hasn't written yet) ---"
  fi
done

echo
echo "Attach to a session with:  tmux attach -t <name>"
echo "Stop one with:            tmux kill-session -t <name>"
echo "Stop all five with:       tmux kill-session -t poller \; kill-session -t paper \; kill-session -t ws \; kill-session -t liq \; kill-session -t ctx"

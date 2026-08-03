"""Run the cascade-rebuild + backtest cycle and update the baseline.

Wraps the four scripts Slim specified:
  scripts/build_cascades.py              --time-window 60 --max-snapshot-lag 120
  scripts/run_fade_or_follow_backtest.py --horizon 15 --wait 3 --max-entry-lag 2
  scripts/run_lane_backtest.py           --lane btc_eth_fade_or_follow
  scripts/run_lane_backtest.py           --lane alt_range_liq_scalp

The first two are the v1 main pipeline and must both succeed before the
baseline is updated. The two lane backtests are best-effort reporting
runs that follow; their failures are logged but do NOT block the baseline
update (they share cascades.jsonl with the main pipeline and the
wrapper does not own the lane strategy logic).

Behavior:
  - default: check trigger; if HOLD, print status and exit 0; if FIRE, run all four
  - --check: just print trigger state, no subprocess calls
  - --force: run all four unconditionally (skips trigger check)
  - --dry-run: print the planned commands without executing
  - --skip-lanes: run only the main pipeline (v1 backtest only)

On both main-pipeline scripts succeeding, the baseline file
(data/.rebuild_baseline.json) is updated via
src.strategy.rebuild_trigger.update_baseline().
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Make `src` importable when run from repo root
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.strategy.rebuild_trigger import (  # noqa: E402
    check_should_rebuild,
    update_baseline,
    THRESHOLD_NEW_ROWS,
    THRESHOLD_LAST_LIQ_AGE_MIN,
    THRESHOLD_LAST_REBUILD_AGE_MIN,
)

PYTHON = ".tools/notebooklm-cli/Scripts/python.exe"
BUILD_CMD = [PYTHON, "scripts/build_cascades.py", "--time-window", "60", "--max-snapshot-lag", "120"]
BACKTEST_CMD = [
    PYTHON,
    "scripts/run_fade_or_follow_backtest.py",
    "--horizon",
    "15",
    "--wait",
    "3",
    "--max-entry-lag",
    "2",
]
LANE_BTC_ETH_CMD = [PYTHON, "scripts/run_lane_backtest.py", "--lane", "btc_eth_fade_or_follow"]
LANE_ALT_CMD = [PYTHON, "scripts/run_lane_backtest.py", "--lane", "alt_range_liq_scalp"]
ALL_CMDS = [BUILD_CMD, BACKTEST_CMD, LANE_BTC_ETH_CMD, LANE_ALT_CMD]


def _print_trigger(info: dict, should_fire: bool) -> None:
    last_liq = info["last_liq_age_min"]
    last_rebuild = info["last_rebuild_age_min"]
    print(
        f"current_liquidation_count: {info['current_liquidation_count']}\n"
        f"new_rows_since_rebuild:    {info['new_rows']} (need >= {THRESHOLD_NEW_ROWS})\n"
        + (
            f"last_liq_age_min:          {last_liq:.1f} (need >= {THRESHOLD_LAST_LIQ_AGE_MIN})\n"
            if last_liq is not None
            else "last_liq_age_min:          None\n"
        )
        + (
            f"last_rebuild_age_min:      {last_rebuild:.1f} (need >= {THRESHOLD_LAST_REBUILD_AGE_MIN})\n"
            if last_rebuild is not None
            else "last_rebuild_age_min:      None (no baseline yet)\n"
        )
        + f"daily_fallback_active:     {info['daily_fallback']}\n"
        + f"reasons:                   {info['reasons'] or 'all conditions met'}\n"
        + f"--> {'FIRE' if should_fire else 'HOLD'}"
    )


def _run(cmd: list[str]) -> int:
    print(f"\n>>> {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print trigger state and exit")
    mode.add_argument("--force", action="store_true", help="run cycle even if trigger is HOLD")
    mode.add_argument("--dry-run", action="store_true", help="print planned commands without running")
    parser.add_argument(
        "--skip-lanes",
        action="store_true",
        help="run only the main pipeline (build_cascades + run_fade_or_follow_backtest); skip lane backtests",
    )
    args = parser.parse_args()

    if args.check:
        should_fire, info = check_should_rebuild()
        _print_trigger(info, should_fire)
        return 0 if should_fire else 1

    should_fire, info = check_should_rebuild()

    planned_cmds = list(ALL_CMDS)
    if args.skip_lanes:
        planned_cmds = [BUILD_CMD, BACKTEST_CMD]

    if args.dry_run:
        print("DRY-RUN: planned commands:")
        for cmd in planned_cmds:
            print("  " + " ".join(cmd))
        return 0

    if not (should_fire or args.force):
        print("TRIGGER HOLD. Skipping rebuild cycle.")
        _print_trigger(info, should_fire)
        return 0

    print("TRIGGER FIRE" if should_fire else "TRIGGER OVERRIDE (--force)")
    _print_trigger(info, should_fire)

    # --- Main pipeline (must both succeed for baseline update) ---
    rc1 = _run(BUILD_CMD)
    if rc1 != 0:
        print(f"build_cascades exited {rc1}; aborting before backtest. Baseline NOT updated.")
        return rc1

    rc2 = _run(BACKTEST_CMD)
    if rc2 != 0:
        print(f"run_fade_or_follow_backtest exited {rc2}; baseline NOT updated.")
        return rc2

    # --- Lane backtests (best-effort, do not block baseline) ---
    lane_failures: list[tuple[str, int]] = []
    if not args.skip_lanes:
        for cmd in (LANE_BTC_ETH_CMD, LANE_ALT_CMD):
            rc = _run(cmd)
            if rc != 0:
                lane_failures.append((" ".join(cmd[1:]), rc))
                print(f"  WARNING: lane backtest exited {rc}; baseline will still update.")
            else:
                print(f"  lane backtest ok.")

    print("\n>>> Main pipeline succeeded. Updating baseline.")
    payload = update_baseline()
    print(f"baseline: {payload}")

    if lane_failures:
        print(f"\n{len(lane_failures)} lane backtest(s) failed (baseline still updated):")
        for cmd_str, rc in lane_failures:
            print(f"  rc={rc}  {cmd_str}")
        return 0  # Don't fail the wrapper on lane-only failures

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

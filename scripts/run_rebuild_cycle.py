"""Run the cascade-rebuild + backtest cycle and update the baseline.

Wraps the six scripts Slim specified:
  scripts/build_cascades.py              --time-window 60 --max-snapshot-lag 120
  scripts/run_fade_or_follow_backtest.py --horizon 15 --wait 3 --max-entry-lag 2
  scripts/run_lane_backtest.py           --lane btc_eth_fade_or_follow
  scripts/run_lane_backtest.py           --lane alt_range_liq_scalp
  scripts/run_lane_backtest.py           --lane btc_eth_fade_or_follow --symbol BTC --side B --diagnostics
  scripts/run_lane_backtest.py           --lane alt_range_liq_scalp         --symbol HYPE --side B --diagnostics

The first two are the v1 main pipeline and must both succeed before the
baseline is updated. The four subsequent runs (two lane sweeps + two
focused side-filtered runs) are best-effort reporting; their failures
are logged but do NOT block the baseline update.

Post-cycle threshold checks (printed as ALERT lines, do not fail the run):
  - BTC B-side reclaim_fade: n >= 75 and PF > 1.5 -> FLAG
  - BTC B-side reclaim_fade: n >= 100 and PF > 1.5 -> FLAG
  - BTC side=B (any variant) total n >= 175 -> FLAG
  - HYPE side=B (any variant) n >= 20 -> FLAG

Behavior:
  - default: check trigger; if HOLD, print status and exit 0; if FIRE, run all six
  - --check: just print trigger state, no subprocess calls
  - --force: run all six unconditionally (skips trigger check)
  - --dry-run: print the planned commands without executing
  - --skip-lanes: run only the main pipeline (v1 backtest only)

On both main-pipeline scripts succeeding, the baseline file
(data/.rebuild_baseline.json) is updated via
src.strategy.rebuild_trigger.update_baseline().
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

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
FOCUSED_BTC_B_CMD = [
    PYTHON,
    "scripts/run_lane_backtest.py",
    "--lane", "btc_eth_fade_or_follow",
    "--symbol", "BTC", "--side", "B",
    "--diagnostics",
]
FOCUSED_HYPE_B_CMD = [
    PYTHON,
    "scripts/run_lane_backtest.py",
    "--lane", "alt_range_liq_scalp",
    "--symbol", "HYPE", "--side", "B",
    "--diagnostics",
]
ALL_CMDS = [
    BUILD_CMD,
    BACKTEST_CMD,
    LANE_BTC_ETH_CMD,
    LANE_ALT_CMD,
    FOCUSED_BTC_B_CMD,
    FOCUSED_HYPE_B_CMD,
]

# Threshold alerts (per Slim's operating rules, 2026-08-03)
BTC_B_RECLAIM_PF_FLAG_THRESHOLDS = [75, 100]
BTC_SIDE_B_TOTAL_N_FLAG_THRESHOLD = 175
HYPE_SIDE_B_TOTAL_N_FLAG_THRESHOLD = 20


def _print_trigger(info: dict, should_fire: bool) -> None:
    last_liq = info["last_liq_age_min"]
    last_rebuild = info["last_rebuild_age_min"]
    print(
        f"current_liquidation_count: {info['current_liquidation_count']}\n"
        f"new_rows_since_rebuild:    {info['new_rows']} (need >= {THRESHOLD_NEW_ROWS})\n"
        f"mature_new_rows:           {info['mature_new_rows']} (need >= {THRESHOLD_NEW_ROWS})\n"
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


def _run_capture(cmd: list[str]) -> tuple[int, str]:
    """Run a subprocess and capture stdout for parsing. Prints as it runs."""
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode, result.stdout


# Pattern: 2+ spaces, variant name, sym, n, WR%, avg, med, PF, optional warning
_SUMMARY_ROW = re.compile(
    r"^\s+(?P<variant>\S+)\s+(?P<sym>\S+)\s+(?P<n>\d+)\s+(?P<wr>\S+)%\s+(?P<avg>\S+)\s+(?P<med>\S+)\s+(?P<pf>\S+|\w+)\s*(?P<warn>.*)"
)


def _parse_pf(pf_str: str) -> Optional[float]:
    """Parse a PF string like '1.65' or 'inf' to float. Returns None on failure."""
    pf_str = pf_str.strip()
    if pf_str.lower() == "inf":
        return float("inf")
    try:
        return float(pf_str)
    except ValueError:
        return None


def _extract_summary_row(output: str, variant: str, sym: str) -> Optional[dict]:
    """Find the summary table row matching (variant, sym) in a lane backtest output.

    Returns dict with keys: variant, sym, n (int), wr (str), avg (str), med (str),
    pf (str), pf_float (float|None), warn (str). Returns None if not found.
    """
    for line in output.splitlines():
        m = _SUMMARY_ROW.match(line)
        if not m:
            continue
        if m.group("variant") == variant and m.group("sym") == sym:
            return {
                "variant": m.group("variant"),
                "sym": m.group("sym"),
                "n": int(m.group("n")),
                "wr": m.group("wr"),
                "avg": m.group("avg"),
                "med": m.group("med"),
                "pf": m.group("pf"),
                "pf_float": _parse_pf(m.group("pf")),
                "warn": m.group("warn").strip(),
            }
    return None


def _sum_variant_rows(output: str, sym: str) -> int:
    """Sum n across all variant rows for a given symbol (e.g. all BTC side=B variants)."""
    total = 0
    for line in output.splitlines():
        m = _SUMMARY_ROW.match(line)
        if not m:
            continue
        if m.group("sym") == sym:
            total += int(m.group("n"))
    return total


def _check_thresholds(btc_b_output: str, hype_b_output: str) -> list[str]:
    """Run the post-cycle threshold checks. Returns a list of alert strings."""
    alerts: list[str] = []

    # BTC B-side reclaim_fade: n>=75 and PF>1.5 -> FLAG; n>=100 and PF>1.5 -> FLAG
    btc_b_reclaim = _extract_summary_row(btc_b_output, "reclaim_fade", "BTC")
    if btc_b_reclaim is not None:
        n = btc_b_reclaim["n"]
        pf = btc_b_reclaim["pf_float"]
        if pf is None:
            alerts.append(
                f"  BTC B-side reclaim_fade: n={n} PF={btc_b_reclaim['pf']} (unparseable)"
            )
        else:
            crossed = [t for t in BTC_B_RECLAIM_PF_FLAG_THRESHOLDS if n >= t and pf > 1.5]
            if crossed:
                thresholds = ", ".join(str(t) for t in crossed)
                alerts.append(
                    f"  FLAG: BTC B-side reclaim_fade n={n}, PF={pf:.2f} (>1.5) "
                    f"crossed threshold(s) [{thresholds}] -- ping Codex"
                )
            else:
                next_t = next(
                    (t for t in BTC_B_RECLAIM_PF_FLAG_THRESHOLDS if t > n),
                    None,
                )
                next_t_str = f", next={next_t}" if next_t is not None else ""
                pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
                alerts.append(
                    f"  BTC B-side reclaim_fade: n={n}, PF={pf_str}{next_t_str} (below threshold)"
                )

    # BTC side=B (any variant) total n >= 175
    btc_b_total = _sum_variant_rows(btc_b_output, "BTC")
    if btc_b_total >= BTC_SIDE_B_TOTAL_N_FLAG_THRESHOLD:
        alerts.append(
            f"  FLAG: BTC side=B total n={btc_b_total} (>= {BTC_SIDE_B_TOTAL_N_FLAG_THRESHOLD}) "
            f"-- ping Codex"
        )
    else:
        alerts.append(
            f"  BTC side=B total: n={btc_b_total} (need >= {BTC_SIDE_B_TOTAL_N_FLAG_THRESHOLD})"
        )

    # HYPE side=B (any variant) n >= 20
    hype_b_total = _sum_variant_rows(hype_b_output, "HYPE")
    if hype_b_total >= HYPE_SIDE_B_TOTAL_N_FLAG_THRESHOLD:
        alerts.append(
            f"  FLAG: HYPE side=B total n={hype_b_total} (>= {HYPE_SIDE_B_TOTAL_N_FLAG_THRESHOLD}) "
            f"-- ping Codex"
        )
    else:
        alerts.append(
            f"  HYPE side=B total: n={hype_b_total} (need >= {HYPE_SIDE_B_TOTAL_N_FLAG_THRESHOLD})"
        )

    return alerts


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
    btc_b_output: str = ""
    hype_b_output: str = ""
    if not args.skip_lanes:
        for cmd in (LANE_BTC_ETH_CMD, LANE_ALT_CMD):
            rc = _run(cmd)
            if rc != 0:
                lane_failures.append((" ".join(cmd[1:]), rc))
                print(f"  WARNING: lane backtest exited {rc}; baseline will still update.")
            else:
                print(f"  lane backtest ok.")

        # --- Focused side-filtered runs (also best-effort, output captured for thresholds) ---
        rc_fb, btc_b_output = _run_capture(FOCUSED_BTC_B_CMD)
        if rc_fb != 0:
            lane_failures.append((" ".join(FOCUSED_BTC_B_CMD[1:]), rc_fb))
            print(f"  WARNING: focused BTC B-side run exited {rc_fb}; baseline will still update.")

        rc_hb, hype_b_output = _run_capture(FOCUSED_HYPE_B_CMD)
        if rc_hb != 0:
            lane_failures.append((" ".join(FOCUSED_HYPE_B_CMD[1:]), rc_hb))
            print(f"  WARNING: focused HYPE B-side run exited {rc_hb}; baseline will still update.")

    print("\n>>> Main pipeline succeeded. Updating baseline.")
    payload = update_baseline()
    print(f"baseline: {payload}")

    if not args.skip_lanes and (btc_b_output or hype_b_output):
        print("\n>>> Threshold checks:")
        for alert in _check_thresholds(btc_b_output, hype_b_output):
            print(alert)

    if lane_failures:
        print(f"\n{len(lane_failures)} lane backtest(s) failed (baseline still updated):")
        for cmd_str, rc in lane_failures:
            print(f"  rc={rc}  {cmd_str}")
        return 0  # Don't fail the wrapper on lane-only failures

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

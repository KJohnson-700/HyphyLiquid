"""Run the cascade-rebuild + backtest cycle and update the baseline.

Wraps the twelve scripts Slim specified:
  scripts/build_cascades.py              --time-window 60 --max-snapshot-lag 120
  scripts/run_fade_or_follow_backtest.py --horizon 15 --wait 3 --max-entry-lag 2
  scripts/run_lane_backtest.py           --lane btc_eth_fade_or_follow
  scripts/run_lane_backtest.py           --lane alt_range_liq_scalp
  scripts/run_lane_backtest.py           --lane btc_eth_fade_or_follow --symbol BTC --side B --diagnostics
  scripts/run_lane_backtest.py           --lane alt_range_liq_scalp         --symbol HYPE --side B --diagnostics
  scripts/run_tp_sl_sweep.py             --lane btc_eth_fade_or_follow --symbol BTC --side B
  scripts/run_tp_sl_sweep.py             --lane btc_eth_fade_or_follow --symbol ETH
  scripts/run_tp_sl_sweep.py             --lane alt_range_liq_scalp         --symbol HYPE --side B
  scripts/run_trailing_sweep.py          --symbol BTC --side B \
                                          --horizons 120,240 --stop-models event_vwap,fixed_bps \
                                          --initial-stops-bps 30,50 --vwap-buffers-bps 15,25 \
                                          --activation-rs 1,1.5,2 --trail-bps 10,15,25 --top 25
  scripts/run_regime_summary.py          (post-pipeline evidence collector per
                                          docs/2026-08-03-HANDOFF-regime-map.md)
  scripts/paper_decision_loop.py         --once --max-new 500

The first two are the v1 main pipeline and must both succeed before the
baseline is updated. The ten subsequent runs (two lane sweeps + two
focused side-filtered + three TP/SL sweeps + one trailing sweep + one
regime summary + one live-like paper update) are best-effort reporting;
their failures are logged but do NOT block the baseline update.

Post-cycle threshold checks (printed as FLAG lines, do not fail the run):
  - BTC B-side reclaim_fade: n >= 75 and PF > 1.5 -> FLAG
  - BTC B-side reclaim_fade: n >= 100 and PF > 1.5 -> FLAG
  - BTC side=B (any variant) total n >= 175 -> FLAG
  - HYPE side=B (any variant) n >= 20 -> FLAG
  - BTC B-side trailing (best row across the sweep): n >= 100, PF > 1.5,
    median_net_return_pct > 0 -> FLAG

Behavior:
  - default: check trigger; if HOLD, print status and exit 0; if FIRE, run all nine
  - --check: just print trigger state, no subprocess calls
  - --force: run all nine unconditionally (skips trigger check)
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
TP_SL_BTC_B_CMD = [
    PYTHON,
    "scripts/run_tp_sl_sweep.py",
    "--lane", "btc_eth_fade_or_follow",
    "--symbol", "BTC", "--side", "B",
]
TP_SL_ETH_CMD = [
    PYTHON,
    "scripts/run_tp_sl_sweep.py",
    "--lane", "btc_eth_fade_or_follow",
    "--symbol", "ETH",
]
TP_SL_HYPE_B_CMD = [
    PYTHON,
    "scripts/run_tp_sl_sweep.py",
    "--lane", "alt_range_liq_scalp",
    "--symbol", "HYPE", "--side", "B",
]
TRAILING_BTC_B_CMD = [
    PYTHON,
    "scripts/run_trailing_sweep.py",
    "--symbol", "BTC",
    "--side", "B",
    "--horizons", "120,240",
    "--stop-models", "event_vwap,fixed_bps",
    "--initial-stops-bps", "30,50",
    "--vwap-buffers-bps", "15,25",
    "--activation-rs", "1,1.5,2",
    "--trail-bps", "10,15,25",
    "--top", "25",
]
REGIME_SUMMARY_CMD = [PYTHON, "scripts/run_regime_summary.py"]
PAPER_DECISION_CMD = [PYTHON, "scripts/paper_decision_loop.py", "--once", "--max-new", "500"]
ALL_CMDS = [
    BUILD_CMD,
    BACKTEST_CMD,
    LANE_BTC_ETH_CMD,
    LANE_ALT_CMD,
    FOCUSED_BTC_B_CMD,
    FOCUSED_HYPE_B_CMD,
    TP_SL_BTC_B_CMD,
    TP_SL_ETH_CMD,
    TP_SL_HYPE_B_CMD,
    TRAILING_BTC_B_CMD,
    REGIME_SUMMARY_CMD,
    PAPER_DECISION_CMD,
]

# Threshold alerts (per Slim's operating rules, 2026-08-03)
BTC_B_RECLAIM_PF_FLAG_THRESHOLDS = [75, 100]
BTC_SIDE_B_TOTAL_N_FLAG_THRESHOLD = 175
HYPE_SIDE_B_TOTAL_N_FLAG_THRESHOLD = 20
BTC_B_TRAILING_N_THRESHOLD = 100
BTC_B_TRAILING_PF_THRESHOLD = 1.5

# Path to the trailing sweep JSON output for BTC B-side (matches
# scripts/run_trailing_sweep.py suffix logic).
TRAILING_BTC_B_JSON = REPO_ROOT / "data" / "trailing_sweep_btc_eth_btc_side_b.json"


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


def _check_trailing_threshold(json_path: Path = TRAILING_BTC_B_JSON) -> list[str]:
    """Check the BTC B-side trailing sweep JSON for Slim's watchlist condition.

    Slim's rule (2026-08-03): best row across the sweep with n >= 100,
    profit_factor > 1.5, and median_net_return_pct > 0 -> FLAG (paper-eligible).

    The trailing sweep sorts display by PF desc, but we re-rank here so the
    analysis is independent of the script's --top cutoff. We require
    stop_model in {fixed_bps, event_vwap} (Slim's specified scope) and the
    row to have at least one valid numeric value. We skip rows that are
    missing any of the threshold fields rather than treating them as 0/None.
    """
    alerts: list[str] = []
    if not json_path.exists():
        return [f"  BTC B-side trailing: JSON missing at {json_path.name} (run not completed?)"]

    try:
        import json as _json
        rows = _json.loads(json_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return [f"  BTC B-side trailing: failed to parse {json_path.name}: {exc}"]

    if not isinstance(rows, list) or not rows:
        return [f"  BTC B-side trailing: {json_path.name} is empty"]

    eligible: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol", "")).upper() != "BTC":
            continue
        if str(row.get("side", row.get("lane", ""))).upper() not in ("B", "BTC_B", "BTC_SIDE_B"):
            # The trailing sweep doesn't tag side per-row, but the JSON
            # filename guarantees the run was --side B. So all rows are
            # implicitly BTC side=B when this function reads the BTC B-side
            # file. Keep this guard for safety in case the file is reused.
            pass
        if str(row.get("stop_model", "")) not in ("fixed_bps", "event_vwap"):
            continue
        try:
            n = int(row.get("n", 0))
            pf = float(row.get("profit_factor", 0.0))
            med = float(row.get("median_net_return_pct", 0.0))
        except (TypeError, ValueError):
            continue
        eligible.append({"n": n, "pf": pf, "med": med, "row": row})

    if not eligible:
        return [f"  BTC B-side trailing: no eligible rows in {json_path.name}"]

    # Rank: highest PF first, tiebreak by n desc, then by med desc.
    eligible.sort(key=lambda e: (-e["pf"], -e["n"], -e["med"]))
    best = eligible[0]
    best_row = best["row"]

    if best["n"] >= BTC_B_TRAILING_N_THRESHOLD and best["pf"] > BTC_B_TRAILING_PF_THRESHOLD and best["med"] > 0:
        alerts.append(
            f"  FLAG: BTC B-side trailing best row "
            f"(variant={best_row.get('variant')}, "
            f"horizon={best_row.get('horizon')}, "
            f"stop={best_row.get('stop_model')}, "
            f"cfg={best_row.get('config_initial_stop_bps') or best_row.get('vwap_buffer_bps') or best_row.get('atr_mult')}, "
            f"actR={best_row.get('activation_r')}, trail={best_row.get('trail_bps')}): "
            f"n={best['n']} PF={best['pf']:.2f} med={best['med']:+.4f}% "
            f"(>= 100 / > 1.5 / > 0) -- consider paper"
        )
    else:
        # Show top 3 for context
        top3 = eligible[:3]
        top3_str = ", ".join(
            f"n={e['n']} PF={e['pf']:.2f} med={e['med']:+.4f}%"
            for e in top3
        )
        alerts.append(
            f"  BTC B-side trailing: best n={best['n']} PF={best['pf']:.2f} "
            f"med={best['med']:+.4f}% (need n>={BTC_B_TRAILING_N_THRESHOLD}, "
            f"PF>{BTC_B_TRAILING_PF_THRESHOLD}, med>0)  top3: [{top3_str}]"
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

        # --- Trailing resolution sweep (BTC B-side, best-effort, JSON read for threshold) ---
        rc_tb, _trailing_b_output = _run_capture(TRAILING_BTC_B_CMD)
        if rc_tb != 0:
            lane_failures.append((" ".join(TRAILING_BTC_B_CMD[1:]), rc_tb))
            print(f"  WARNING: trailing BTC B-side run exited {rc_tb}; baseline will still update.")

        # --- Regime summary (per regime-map handoff; appends to data/regime_log/) ---
        rc_rs = _run(REGIME_SUMMARY_CMD)
        if rc_rs != 0:
            lane_failures.append((" ".join(REGIME_SUMMARY_CMD[1:]), rc_rs))
            print(f"  WARNING: regime summary exited {rc_rs}; baseline will still update.")
        else:
            print("  regime summary ok.")

        # --- Live-like paper lane update (best-effort, no exchange calls) ---
        rc_pd = _run(PAPER_DECISION_CMD)
        if rc_pd != 0:
            lane_failures.append((" ".join(PAPER_DECISION_CMD[1:]), rc_pd))
            print(f"  WARNING: paper decision loop exited {rc_pd}; baseline will still update.")
        else:
            print("  paper decision loop ok.")

    print("\n>>> Main pipeline succeeded. Updating baseline.")
    payload = update_baseline()
    print(f"baseline: {payload}")

    if not args.skip_lanes and (btc_b_output or hype_b_output):
        print("\n>>> Threshold checks:")
        for alert in _check_thresholds(btc_b_output, hype_b_output):
            print(alert)

    if not args.skip_lanes:
        print("\n>>> Trailing resolution threshold check:")
        for alert in _check_trailing_threshold():
            print(alert)

    if lane_failures:
        print(f"\n{len(lane_failures)} lane backtest(s) failed (baseline still updated):")
        for cmd_str, rc in lane_failures:
            print(f"  rc={rc}  {cmd_str}")
        return 0  # Don't fail the wrapper on lane-only failures

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

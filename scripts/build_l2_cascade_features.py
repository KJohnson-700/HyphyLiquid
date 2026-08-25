"""Join per-event L2 depth features with cascade events (research only).

Reads:
  - data/cascades.jsonl                                       (one cascade per line)
  - data/l2_depth_features/{symbol}_{YYYY-MM-DD}.jsonl        (per-event features)

Writes:
  - data/l2_cascade_features/{symbol}_{YYYY-MM-DD}.jsonl
    One record per cascade that has at least an L2 file for its date.
    Each record carries four L2 snapshots (t-30s, t+5s, t+30s, t+60s
    relative to cascade event_ts_ms), plus pre-cascade "thinning" and
    post-cascade "resilience" derived features.

Join key: cascade.event_ts_ms <-> l2.ts_ms (HL exchange clock, per Slim 2026-08-07).

Why four snapshots, not one: cascade attacks on book depth are short-lived
(1-3s), recovery starts 5-10s later, full rebalancing by 30-60s. A single
t+0 snapshot misses both the pre-attack book and the post-recovery state.
t-30s gives the calm pre-cascade baseline; t+5s captures the immediate
post-attack impact; t+30s and t+60s measure the recovery path.

HARD SCOPE: research only. Does not touch execution, order_manager, risk.py,
paper routing, or live/paper/live routing. BTC/ETH/SOL only (the symbols
with L2 coverage; HYPE has no L2 data, HIP-3 assets excluded).

Stream-processing: l2 input files are loaded per (symbol, date) into a
sorted list (typically 16k-32k entries per day, well within memory), then
bisected for each cascade. Cascades are grouped by (symbol, date) so each
l2 file is read at most once.

Usage:
    python scripts/build_l2_cascade_features.py --symbol BTC ETH SOL
    python scripts/build_l2_cascade_features.py --symbol BTC --start 2026-08-06 --end 2026-08-07
    python scripts/build_l2_cascade_features.py --all
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.strategy.event_features import _canonical_symbol, _file_stem  # noqa: E402

# --- Paths ---
CASCADES_PATH = REPO_ROOT / "data" / "cascades.jsonl"
L2_INPUT_DIR = REPO_ROOT / "data" / "l2_depth_features"
OUTPUT_DIR = REPO_ROOT / "data" / "l2_cascade_features"
LOG_PATH = REPO_ROOT / "logs" / "l2_cascade_features.log"


def _log_path() -> Path:
    """Resolve the log file at call time, honouring HYPHYLIQUID_LOG_DIR.

    LOG_PATH is bound at import against the real repo, so a test that
    repoints the data paths but forgets this one writes into the live
    logs/ directory -- which is how pytest runs ended up interleaved with
    production output in logs/l2_cascade_features.log. conftest.py points
    the env var at a tmp dir for the whole suite so no test can do it.
    """
    base = os.environ.get("HYPHYLIQUID_LOG_DIR")
    return (Path(base) / "l2_cascade_features.log") if base else LOG_PATH

# --- Snapshot offsets relative to cascade event_ts_ms (ms) ---
OFFSET_T_MINUS_30S_MS: int = -30_000
OFFSET_T_PLUS_5S_MS: int = 5_000
OFFSET_T_PLUS_30S_MS: int = 30_000
OFFSET_T_PLUS_60S_MS: int = 60_000

# --- Snapshot tolerance: if no L2 entry within this many ms of the target
#     timestamp, the snapshot is considered "missing" (book not updating
#     at the expected cadence or coverage gap). Returns null fields. ---
SNAPSHOT_TOLERANCE_MS: int = 60_000  # 60s

# --- Levels to compute thinning/resilience for ---
OBI_LEVELS: Tuple[int, ...] = (5, 10, 20)
DEPTH_LEVELS: Tuple[int, ...] = (5, 10, 20)

# --- L2 feature fields we propagate to each snapshot ---
SNAPSHOT_FIELDS: Tuple[str, ...] = (
    "ts_ms",
    "recv_ts",
    "mid",
    "spread_bps",
    "lag_ms",
    "stale_book_flag",
    "mid_drift_bps",
    "depth_top5_bid",
    "depth_top5_ask",
    "depth_top10_bid",
    "depth_top10_ask",
    "depth_top20_bid",
    "depth_top20_ask",
    "obi_5",
    "obi_10",
    "obi_20",
    "ofi_5_instant",
    "ofi_10_instant",
    "ofi_5_5s",
    "ofi_5_30s",
    "ofi_10_5s",
    "ofi_10_30s",
)

# --- Default symbols (per Slim 2026-08-07: BTC/ETH/SOL only) ---
DEFAULT_SYMBOLS: Tuple[str, ...] = ("BTC", "ETH", "SOL")


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> logging.Logger:
    log_file = _log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("build_l2_cascade_features")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        ))
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        log.addHandler(sh)
    return log


# ----------------------------------------------------------------------------
# Math helpers (all divide-by-zero safe)
# ----------------------------------------------------------------------------

def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    if den is None or den == 0:
        return default
    return num / den


def _safe_ratio_diff(post: Optional[float], pre: Optional[float]) -> Optional[float]:
    """(post - pre) / pre. None if pre is None, 0, or post is None.

    Used for thinning (depth drop, spread widen) and recovery metrics.
    Sign convention:
      - depth_top5_bid_thinning = negative when bid side thinned (post < pre)
      - spread_widen = positive when spread grew (post > pre)
      - obi_drop = post - pre (signed, not ratio)
    """
    if pre is None or post is None:
        return None
    if pre == 0:
        return None
    return (post - pre) / pre


def _safe_signed_diff(post: Optional[float], pre: Optional[float]) -> Optional[float]:
    """post - pre. None if either is None."""
    if pre is None or post is None:
        return None
    return post - pre


# ----------------------------------------------------------------------------
# L2 file I/O
# ----------------------------------------------------------------------------

def _load_l2_file(path: Path, log: logging.Logger) -> List[dict]:
    """Stream-read an L2 feature file, return rows sorted by ts_ms.

    Skips malformed lines but logs them. Returns [] if file is missing/empty.
    """
    if not path.exists():
        return []
    rows: List[dict] = []
    bad = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                bad += 1
                continue
            if "ts_ms" not in obj:
                bad += 1
                continue
            rows.append(obj)
    rows.sort(key=lambda r: r["ts_ms"])
    if bad:
        log.warning("L2 file %s: %d bad/skipped lines", path.name, bad)
    return rows


def _bisect_snapshot(
    sorted_rows: List[dict],
    target_ts_ms: int,
    tolerance_ms: int = SNAPSHOT_TOLERANCE_MS,
) -> Optional[dict]:
    """Find the L2 snapshot nearest to target_ts_ms.

    Strategy: take the first row with ts_ms >= target_ts_ms (bisect_right - 1
    if exact match, else bisect_left). If the gap between the chosen row and
    the target exceeds tolerance_ms, return None.

    Returned row is a SHALLOW copy with only SNAPSHOT_FIELDS, so the caller
    cannot accidentally mutate the source list.
    """
    if not sorted_rows:
        return None
    ts_list = [r["ts_ms"] for r in sorted_rows]
    idx = bisect.bisect_left(ts_list, target_ts_ms)
    candidates: List[int] = []
    if idx < len(sorted_rows):
        candidates.append(idx)
    if idx > 0:
        candidates.append(idx - 1)
    if not candidates:
        return None
    # Pick the candidate closest in time to target.
    best_idx = min(candidates, key=lambda i: abs(sorted_rows[i]["ts_ms"] - target_ts_ms))
    chosen = sorted_rows[best_idx]
    if abs(chosen["ts_ms"] - target_ts_ms) > tolerance_ms:
        return None
    return {k: chosen.get(k) for k in SNAPSHOT_FIELDS}


# ----------------------------------------------------------------------------
# Cascade I/O
# ----------------------------------------------------------------------------

def _load_cascades(
    path: Path,
    symbols: Tuple[str, ...],
    start_date: Optional[str],
    end_date: Optional[str],
    log: logging.Logger,
) -> Dict[Tuple[str, str], List[dict]]:
    """Read cascades.jsonl, filter by symbol and date range, group by (symbol, date).

    Returns a dict mapping (symbol_upper, YYYY-MM-DD) -> list of cascade records.
    Records outside the date range are filtered out.
    Records whose event_ts_ms is missing are dropped with a warning.
    """
    if not path.exists():
        log.error("cascades.jsonl not found at %s", path)
        return {}
    sym_set = {s.upper() for s in symbols}
    out: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    n_total = 0
    n_kept = 0
    n_no_ts = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_total += 1
            sym = rec.get("symbol")
            if not sym or sym.upper() not in sym_set:
                continue
            ts_ms = rec.get("event_ts_ms")
            if ts_ms is None:
                n_no_ts += 1
                continue
            date_str = rec.get("event_ts", "")[:10] or _iso_date_from_ms(int(ts_ms))
            if start_date and date_str < start_date:
                continue
            if end_date and date_str > end_date:
                continue
            out[(sym.upper(), date_str)].append(rec)
            n_kept += 1
    log.info(
        "cascades: %d total, %d kept, %d missing event_ts_ms, %d (symbol,date) groups",
        n_total,
        n_kept,
        n_no_ts,
        len(out),
    )
    return out


def _iso_date_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# Per-cascade feature computation
# ----------------------------------------------------------------------------

def _build_cascade_record(
    cascade: dict,
    l2_rows: List[dict],
    l2_path: Path,
) -> dict:
    """Build the joined cascade record with snapshots + derived features.

    If l2_rows is empty, the record is still emitted with null snapshots
    and null derived features (so downstream backtesters can count "no
    L2 coverage" as a separate bucket).
    """
    event_ts_ms = int(cascade["event_ts_ms"])
    sym = cascade.get("symbol", "").upper()

    snap_t30 = _bisect_snapshot(l2_rows, event_ts_ms + OFFSET_T_MINUS_30S_MS)
    snap_p5 = _bisect_snapshot(l2_rows, event_ts_ms + OFFSET_T_PLUS_5S_MS)
    snap_p30 = _bisect_snapshot(l2_rows, event_ts_ms + OFFSET_T_PLUS_30S_MS)
    snap_p60 = _bisect_snapshot(l2_rows, event_ts_ms + OFFSET_T_PLUS_60S_MS)

    # Pre-cascade "thinning" — book deformation from t-30s baseline to t+5s
    # after the cascade attack. Negative obi_drop = book flipped toward
    # cascade direction. Negative depth_drop = book thinned.
    pre: Dict[str, Any] = {}
    for n in OBI_LEVELS:
        pre[f"obi_{n}_drop"] = _safe_signed_diff(
            (snap_p5 or {}).get(f"obi_{n}"),
            (snap_t30 or {}).get(f"obi_{n}"),
        )
    for n in DEPTH_LEVELS:
        for side in ("bid", "ask"):
            pre[f"depth_top{n}_{side}_drop"] = _safe_ratio_diff(
                (snap_p5 or {}).get(f"depth_top{n}_{side}"),
                (snap_t30 or {}).get(f"depth_top{n}_{side}"),
            )
    pre["spread_widen"] = _safe_ratio_diff(
        (snap_p5 or {}).get("spread_bps"),
        (snap_t30 or {}).get("spread_bps"),
    )
    pre["ofi_5_30s_magnitude"] = (snap_p5 or {}).get("ofi_5_30s")
    pre["ofi_10_30s_magnitude"] = (snap_p5 or {}).get("ofi_10_30s")

    # Post-cascade "resilience" — book recovery from t+5s to t+60s.
    # Positive obi_recovery = book rebalances back. Positive depth_recovery
    # = depth reloaded. Positive spread_recovery = spread tightens back.
    post: Dict[str, Any] = {}
    for n in OBI_LEVELS:
        post[f"obi_{n}_recovery"] = _safe_signed_diff(
            (snap_p60 or {}).get(f"obi_{n}"),
            (snap_p5 or {}).get(f"obi_{n}"),
        )
    for n in DEPTH_LEVELS:
        for side in ("bid", "ask"):
            post[f"depth_top{n}_{side}_recovery"] = _safe_ratio_diff(
                (snap_p60 or {}).get(f"depth_top{n}_{side}"),
                (snap_p5 or {}).get(f"depth_top{n}_{side}"),
            )
    post["spread_recovery"] = _safe_ratio_diff(
        (snap_p60 or {}).get("spread_bps"),
        (snap_p5 or {}).get("spread_bps"),
    )
    # 30s mid-waypoint snapshots (informational; used by backtesters
    # that want a more granular recovery path).
    post["obi_5_30s_recovery"] = _safe_signed_diff(
        (snap_p30 or {}).get("obi_5"),
        (snap_p5 or {}).get("obi_5"),
    )
    post["depth_top5_bid_30s_recovery"] = _safe_ratio_diff(
        (snap_p30 or {}).get("depth_top5_bid"),
        (snap_p5 or {}).get("depth_top5_bid"),
    )

    record: Dict[str, Any] = dict(cascade)  # copy original cascade
    record["_l2_input_path"] = l2_path.name
    record["_snapshots_present"] = sum(
        1 for s in (snap_t30, snap_p5, snap_p30, snap_p60) if s is not None
    )
    record["snapshot_t_minus_30s"] = snap_t30
    record["snapshot_t_plus_5s"] = snap_p5
    record["snapshot_t_plus_30s"] = snap_p30
    record["snapshot_t_plus_60s"] = snap_p60
    record["pre_thinning"] = pre
    record["post_resilience"] = post
    return record


# ----------------------------------------------------------------------------
# Per-(symbol,date) processing
# ----------------------------------------------------------------------------

def process_one_date(
    symbol: str,
    date_str: str,
    cascades: List[dict],
    l2_input_dir: Path,
    out_dir: Path,
    log: logging.Logger,
) -> Tuple[int, int, int]:
    """Process one (symbol, date) pair: load L2 file, join, write.

    Returns (n_cascades_in, n_written, n_dropped).
    """
    l2_path = l2_input_dir / f"{_file_stem(symbol)}_{date_str}.jsonl"
    l2_rows = _load_l2_file(l2_path, log)
    log.info(
        "[%s %s] cascades=%d, l2_rows=%d (path=%s)",
        symbol,
        date_str,
        len(cascades),
        len(l2_rows),
        l2_path.name if l2_rows else f"{l2_path.name} (missing)",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_file_stem(symbol)}_{date_str}.jsonl"

    n_written = 0
    n_dropped = 0
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        for c in cascades:
            try:
                rec = _build_cascade_record(c, l2_rows, l2_path)
                f.write(json.dumps(rec) + "\n")
                n_written += 1
            except Exception as e:  # noqa: BLE001
                n_dropped += 1
                log.warning(
                    "[%s %s] drop cascade event_ts_ms=%s: %s",
                    symbol,
                    date_str,
                    c.get("event_ts_ms"),
                    e,
                )
    # Atomic rename.
    tmp_path.replace(out_path)
    return len(cascades), n_written, n_dropped


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

def discover_pairs(
    symbols: Tuple[str, ...],
    start_date: Optional[str],
    end_date: Optional[str],
) -> List[Tuple[str, str, Path]]:
    """Find all (symbol, date, l2_path) triples with existing L2 data.

    Used when --all is passed: scan the l2 input dir, filter by symbols
    and date range, return one triple per (symbol, date).
    """
    out: List[Tuple[str, str, Path]] = []
    if not L2_INPUT_DIR.exists():
        return out
    for path in sorted(L2_INPUT_DIR.glob("*.jsonl")):
        stem = path.stem
        # Stem is "{symbol_lower}_{YYYY-MM-DD}"; symbol can contain
        # underscores (e.g. "xyz_gold"), so we anchor the date at the end.
        parts = stem.rsplit("_", 1)
        if len(parts) != 2:
            continue
        sym_lower, date_str = parts
        sym_upper = sym_lower.upper()
        if sym_upper not in {s.upper() for s in symbols}:
            continue
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        out.append((sym_upper, date_str, path))
    return out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Join L2 depth features with cascade events (research only)."
    )
    parser.add_argument(
        "--symbol",
        nargs="+",
        default=None,
        help="Symbols to process (uppercase). Default: BTC ETH SOL",
    )
    parser.add_argument("--all", action="store_true", help="Process every (symbol,date) in l2 input dir")
    parser.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD inclusive")
    parser.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD inclusive")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    log = _setup_logging(verbose=args.verbose)

    symbols: Tuple[str, ...] = tuple(s.upper() for s in (args.symbol or DEFAULT_SYMBOLS))
    log.info("symbols=%s, start=%s, end=%s, all=%s", symbols, args.start, args.end, args.all)

    cascades_by_pair = _load_cascades(
        CASCADES_PATH, symbols, args.start, args.end, log
    )

    if args.all:
        # Drive from l2 files: every (symbol, date) with l2 data gets
        # processed, even if no cascades that day.
        pairs = discover_pairs(symbols, args.start, args.end)
        log.info("discovered %d (symbol,date) pairs from L2 input dir", len(pairs))
    else:
        # Drive from cascades: only (symbol, date) with at least one cascade.
        pairs = [
            (sym, date, L2_INPUT_DIR / f"{_file_stem(sym)}_{date}.jsonl")
            for (sym, date) in sorted(cascades_by_pair.keys())
        ]
        log.info("using %d (symbol,date) pairs from cascades.jsonl", len(pairs))

    if not pairs:
        log.warning("no (symbol,date) pairs to process")
        return 0

    total_cascades = 0
    total_written = 0
    total_dropped = 0
    files_written = 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for sym, date_str, _l2_path in pairs:
        cascades = cascades_by_pair.get((sym, date_str), [])
        n_in, n_out, n_drop = process_one_date(
            sym, date_str, cascades, L2_INPUT_DIR, OUTPUT_DIR, log
        )
        total_cascades += n_in
        total_written += n_out
        total_dropped += n_drop
        files_written += 1

    log.info(
        "done: %d files, cascades_in=%d, written=%d, dropped=%d",
        files_written,
        total_cascades,
        total_written,
        total_dropped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

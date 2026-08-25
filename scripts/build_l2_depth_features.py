"""Build per-event depth features from raw L2 book events.

Reads data/ws_l2book/{symbol}_{YYYY-MM-DD}.jsonl and writes
data/l2_depth_features/{symbol}_{YYYY-MM-DD}.jsonl with derived features
for downstream cascade analysis.

Per the Tier-2 shortlist (2026-08-07, Slim), the features are:
  - mid, spread_bps
  - depth_topN_{bid,ask} for N in {1, 5, 10, 20}
  - obi_N for N in {1, 5, 10, 20}                       # signed imbalance [-1, 1]
  - ofi_N_instant for N in {5, 10}                      # Cont-Kukanov-Stoikov, single snapshot
  - ofi_{N}_{window_s}s for N in {5,10}, window in {5, 30}  # windowed cumulative
  - stale_book_flag                                    # spread > 1.5x rolling 5min median
                                                       # AND mid drift over 5min < 1 bps
  - stale_lag_ms                                       # recv_ts - ts_ms (diagnostic)

Joins downstream use payload.time (the HL exchange clock), per Slim 2026-08-07
directive. recv_ts is retained for stale/lag diagnostics only.

Stream-processing: never loads the full input file. Per-symbol stateful (one
SymbolState per symbol, O(1) prev + O(window_size) deques).

BTC/ETH/SOL only per Slim 2026-08-07 directive.

Usage:
    python scripts/build_l2_depth_features.py --symbol BTC ETH SOL
    python scripts/build_l2_depth_features.py --symbol BTC --start 2026-08-07 --end 2026-08-07
    python scripts/build_l2_depth_features.py --all  # process every (symbol, date) in input dir
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Iterator, List, Optional, Tuple

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[1]
L2_INPUT_DIR = REPO_ROOT / "data" / "ws_l2book"
L2_OUTPUT_DIR = REPO_ROOT / "data" / "l2_depth_features"
LOG_PATH = REPO_ROOT / "logs" / "l2_depth_features.log"


def _log_path() -> Path:
    """Resolve the log file at call time, honouring HYPHYLIQUID_LOG_DIR.

    LOG_PATH is bound at import against the real repo, so a test that
    repoints the data paths but forgets this one writes into the live
    logs/ directory -- which is how pytest runs ended up interleaved with
    production output in logs/l2_cascade_features.log. conftest.py points
    the env var at a tmp dir for the whole suite so no test can do it.
    """
    base = os.environ.get("HYPHYLIQUID_LOG_DIR")
    return (Path(base) / "l2_depth_features.log") if base else LOG_PATH

# --- Feature config ---
OBI_LEVELS: Tuple[int, ...] = (1, 5, 10, 20)
OFI_LEVELS: Tuple[int, ...] = (5, 10)
OFI_WINDOW_S: Tuple[int, ...] = (5, 30)
STALE_WINDOW_S: int = 300
STALE_SPREAD_MULT: float = 1.5
STALE_MID_DRIFT_BPS: float = 1.0
STALE_MIN_HISTORY: int = 5

# --- Default symbols (per Slim 2026-08-07: BTC/ETH/SOL only) ---
DEFAULT_SYMBOLS: Tuple[str, ...] = ("BTC", "ETH", "SOL")

# Filename pattern: {symbol_lower}_{YYYY-MM-DD}.jsonl
# Symbol may contain underscores (e.g. xyz_gold, xyz_silver), so we anchor
# the date at the end of the stem.
FILENAME_DATE_RE = re.compile(r"^(.+)_(\d{4}-\d{2}-\d{2})\.jsonl$")


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> logging.Logger:
    log_file = _log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("build_l2_depth_features")
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
# Event parsing
# ----------------------------------------------------------------------------

def _parse_event(line: str) -> Optional[dict]:
    """Parse a single JSONL line from ws_l2book.

    Returns a flat dict with the fields we need, or None on bad data.
    Skips events without a usable payload (no levels, bad json, etc).
    """
    try:
        obj = json.loads(line)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        return None
    bids, asks = levels[0], levels[1]
    if not isinstance(bids, list) or not isinstance(asks, list):
        return None
    return {
        "recv_ts": obj.get("recv_ts"),
        "ts_ms": payload.get("time") or payload.get("ts"),
        "coin": payload.get("coin"),
        "spread_str": payload.get("spread", "0"),
        "bids": bids,
        "asks": asks,
    }


def _parse_iso_ms(s: Optional[str]) -> int:
    """Parse ISO 8601 string to Unix ms. Returns 0 on failure."""
    if not s or not isinstance(s, str):
        return 0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


# ----------------------------------------------------------------------------
# Math helpers
# ----------------------------------------------------------------------------

def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    """Divide, returning `default` on zero denominator or non-finite result."""
    if den == 0:
        return default
    out = num / den
    if out != out:  # NaN check
        return default
    return out


def _depth(levels: List[dict], n: int) -> float:
    """Sum size for top n levels. Returns 0.0 if no levels."""
    if not levels:
        return 0.0
    total = 0.0
    for lvl in levels[:n]:
        try:
            total += float(lvl.get("sz", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _obi(bid_depth: float, ask_depth: float) -> float:
    """Signed imbalance in [-1, 1]. Returns 0.0 if both sides are empty."""
    return _safe_div(bid_depth - ask_depth, bid_depth + ask_depth, default=0.0)


def _spread_bps(best_bid: float, best_ask: float) -> float:
    """Spread in basis points. Returns 0.0 if mid is zero or negative."""
    if best_bid <= 0 or best_ask <= 0:
        return 0.0
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return 0.0
    return (best_ask - best_bid) / mid * 10000.0


def _mid(best_bid: float, best_ask: float) -> float:
    if best_bid <= 0 or best_ask <= 0:
        return 0.0
    return (best_bid + best_ask) / 2.0


def _ofi_instant(
    curr_bids: List[dict],
    curr_asks: List[dict],
    prev_bids: Optional[List[dict]],
    prev_asks: Optional[List[dict]],
    n: int,
) -> float:
    """Single-snapshot Cont-Kukanov-Stoikov OFI for top n levels.

    OFI = Σ(Δbid_sz_k - Δask_sz_k) for k in [0, n).
    Returns 0.0 if no previous snapshot.

    Sign convention: bid growing is positive (signals buying pressure),
    ask growing is negative (signals selling pressure). The Δbid-Δask
    difference measures net aggressive flow in the level queue.
    """
    if prev_bids is None or prev_asks is None:
        return 0.0
    out = 0.0
    for k in range(n):
        curr_bid_sz = _safe_level_sz(curr_bids, k)
        curr_ask_sz = _safe_level_sz(curr_asks, k)
        prev_bid_sz = _safe_level_sz(prev_bids, k)
        prev_ask_sz = _safe_level_sz(prev_asks, k)
        out += (curr_bid_sz - prev_bid_sz) - (curr_ask_sz - prev_ask_sz)
    return out


def _safe_level_sz(levels: List[dict], k: int) -> float:
    """Read size from level k, or 0.0 if k is out of range or value is invalid."""
    if k >= len(levels):
        return 0.0
    try:
        return float(levels[k].get("sz", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _lag_ms(recv_ts_str: Optional[str], ts_ms: Optional[int]) -> int:
    """recv_ts - ts_ms in ms. Returns 0 if either is missing or zero."""
    if not recv_ts_str or not ts_ms:
        return 0
    recv_ms = _parse_iso_ms(recv_ts_str)
    if recv_ms == 0 or ts_ms == 0:
        return 0
    return recv_ms - ts_ms


# ----------------------------------------------------------------------------
# Per-symbol streaming state
# ----------------------------------------------------------------------------

class SymbolState:
    """Stateful per-symbol feature calculator.

    Holds:
      - prev_bids / prev_asks: last event's top-of-book levels
      - ofi_windows[N][ws_s]: deque of (ts_ms, ofi_value) for each OFI level/window
      - spread_mid_window: deque of (ts_ms, spread_bps, mid) for stale-book detection
    """

    def __init__(self) -> None:
        self.prev_bids: Optional[List[dict]] = None
        self.prev_asks: Optional[List[dict]] = None
        self.ofi_windows: Dict[int, Dict[int, Deque[Tuple[int, float]]]] = {
            N: {ws_s: deque() for ws_s in OFI_WINDOW_S} for N in OFI_LEVELS
        }
        self.spread_mid_window: Deque[Tuple[int, float, float]] = deque()

    def update_ofi(self, ts_ms: int, ofi_instant: Dict[int, float]) -> None:
        for N, val in ofi_instant.items():
            for ws_s, dq in self.ofi_windows[N].items():
                dq.append((ts_ms, val))
                self._evict(dq, ts_ms, ws_s * 1000)

    def get_windowed_ofi(self) -> Dict[int, Dict[int, float]]:
        out: Dict[int, Dict[int, float]] = {}
        for N in OFI_LEVELS:
            out[N] = {ws_s: sum(v for _, v in dq) for ws_s, dq in self.ofi_windows[N].items()}
        return out

    def update_spread_mid(self, ts_ms: int, spread_bps: float, mid: float) -> None:
        self.spread_mid_window.append((ts_ms, spread_bps, mid))
        self._evict(self.spread_mid_window, ts_ms, STALE_WINDOW_S * 1000)

    def check_stale(
        self, curr_spread_bps: float, curr_mid: float
    ) -> Tuple[bool, float]:
        """Returns (is_stale, mid_drift_bps)."""
        if len(self.spread_mid_window) < STALE_MIN_HISTORY:
            return False, 0.0
        spreads = [s for _, s, _ in self.spread_mid_window]
        try:
            median_spread = statistics.median(spreads)
        except statistics.StatisticsError:
            return False, 0.0
        if median_spread <= 0:
            return False, 0.0
        mids = [m for _, _, m in self.spread_mid_window]
        mid_drift_abs = max(mids) - min(mids)
        denom = max(curr_mid, 1e-9)
        mid_drift_bps = mid_drift_abs / denom * 10000.0
        is_stale = (curr_spread_bps > STALE_SPREAD_MULT * median_spread) and (
            mid_drift_bps < STALE_MID_DRIFT_BPS
        )
        return is_stale, mid_drift_bps

    @staticmethod
    def _evict(dq: Deque, current_ts_ms: int, window_ms: int) -> None:
        cutoff = current_ts_ms - window_ms
        while dq and dq[0][0] < cutoff:
            dq.popleft()


# ----------------------------------------------------------------------------
# File processing
# ----------------------------------------------------------------------------

def _extract_date_from_filename(path: Path) -> Optional[str]:
    """Pull YYYY-MM-DD out of '{symbol}_{YYYY-MM-DD}.jsonl' filename."""
    m = FILENAME_DATE_RE.match(path.name)
    if not m:
        return None
    return m.group(2)


def process_file(
    symbol: str,
    date_str: str,
    input_path: Path,
    output_path: Path,
    log: logging.Logger,
) -> int:
    """Process one (symbol, date) input file. Returns number of features written.

    Skips events whose coin does not match the expected symbol (defensive;
    in practice a file is named for one symbol, but the payload also has
    `coin` and we trust that).
    """
    if not input_path.exists():
        log.warning("input missing: %s", input_path)
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state = SymbolState()
    n = 0
    n_skip = 0
    with input_path.open("r", encoding="utf-8") as fin, output_path.open(
        "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            if not line.strip():
                continue
            ev = _parse_event(line)
            if ev is None:
                n_skip += 1
                continue
            if ev["coin"] != symbol:
                n_skip += 1
                continue
            if ev is None:
                n_skip += 1
                continue
            if ev["coin"] != symbol:
                n_skip += 1
                continue
            bids = ev["bids"]
            asks = ev["asks"]
            if not bids or not asks:
                n_skip += 1
                continue
            try:
                best_bid = float(bids[0]["px"])
                best_ask = float(asks[0]["px"])
            except (TypeError, ValueError, KeyError):
                n_skip += 1
                continue
            mid = _mid(best_bid, best_ask)
            spread_bps = _spread_bps(best_bid, best_ask)
            ts_ms = ev["ts_ms"] or 0
            recv_ts_ms = _parse_iso_ms(ev["recv_ts"])
            if recv_ts_ms == 0:
                # Fall back to ts_ms so windowed features still work.
                # recv_ts is retained in the output as-is for diagnostics.
                recv_ts_ms = ts_ms
            # Depth at each OBI level
            depths: Dict[int, Tuple[float, float]] = {
                N: (_depth(bids, N), _depth(asks, N)) for N in OBI_LEVELS
            }
            obis: Dict[int, float] = {
                N: _obi(depths[N][0], depths[N][1]) for N in OBI_LEVELS
            }
            # OFI instant
            ofi_instant: Dict[int, float] = {
                N: _ofi_instant(bids, asks, state.prev_bids, state.prev_asks, N)
                for N in OFI_LEVELS
            }
            # Windowed OFI (update first, then read; includes current instant)
            state.update_ofi(recv_ts_ms, ofi_instant)
            ofi_windowed = state.get_windowed_ofi()
            # Stale book
            state.update_spread_mid(recv_ts_ms, spread_bps, mid)
            is_stale, mid_drift_bps = state.check_stale(spread_bps, mid)
            # Lag
            lag_ms = _lag_ms(ev["recv_ts"], ts_ms)
            # Build output record
            out = {
                "recv_ts": ev["recv_ts"],
                "ts_ms": ts_ms,
                "coin": symbol,
                "mid": mid,
                "spread_bps": spread_bps,
                "lag_ms": lag_ms,
                "stale_book_flag": is_stale,
                "mid_drift_bps": mid_drift_bps,
            }
            for N, (bid_d, ask_d) in depths.items():
                out[f"depth_top{N}_bid"] = bid_d
                out[f"depth_top{N}_ask"] = ask_d
                out[f"obi_{N}"] = obis[N]
                if N in ofi_instant:
                    out[f"ofi_{N}_instant"] = ofi_instant[N]
            for N in OFI_LEVELS:
                for ws_s in OFI_WINDOW_S:
                    out[f"ofi_{N}_{ws_s}s"] = ofi_windowed[N][ws_s]
            fout.write(json.dumps(out) + "\n")
            # Update prev for next iteration
            state.prev_bids = bids
            state.prev_asks = asks
            n += 1
    log.info(
        "%s %s: wrote %d features (skipped %d bad/empty)",
        symbol,
        date_str,
        n,
        n_skip,
    )
    return n


def discover_inputs(symbols: Tuple[str, ...]) -> List[Tuple[str, str, Path]]:
    """Return [(symbol, date_str, input_path), ...] sorted by (symbol, date).

    Scans L2_INPUT_DIR for files matching {symbol_lower}_{YYYY-MM-DD}.jsonl
    across all requested symbols.
    """
    found: List[Tuple[str, str, Path]] = []
    for sym in symbols:
        for path in L2_INPUT_DIR.glob(f"{sym.lower()}_*.jsonl"):
            date_str = _extract_date_from_filename(path)
            if date_str:
                found.append((sym, date_str, path))
    found.sort(key=lambda x: (x[0], x[1]))
    return found


def filter_by_date_range(
    items: List[Tuple[str, str, Path]],
    start: Optional[str],
    end: Optional[str],
) -> List[Tuple[str, str, Path]]:
    if not start and not end:
        return items
    out = []
    for sym, date_str, path in items:
        if start and date_str < start:
            continue
        if end and date_str > end:
            continue
        out.append((sym, date_str, path))
    return out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build per-event L2 depth features (OBI, OFI, stale, lag) from ws_l2book.",
    )
    p.add_argument(
        "--symbol",
        nargs="+",
        default=None,
        help="One or more symbols to process (default: BTC ETH SOL).",
    )
    p.add_argument(
        "--start",
        default=None,
        help="Inclusive start date YYYY-MM-DD (default: all available).",
    )
    p.add_argument(
        "--end",
        default=None,
        help="Inclusive end date YYYY-MM-DD (default: all available).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Process every (symbol, date) in the input dir. Overrides --symbol/--start/--end.",
    )
    p.add_argument(
        "--input-dir",
        type=Path,
        default=L2_INPUT_DIR,
        help="Override input directory (default: data/ws_l2book).",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=L2_OUTPUT_DIR,
        help="Override output directory (default: data/l2_depth_features).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Debug logging.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    log = _setup_logging(verbose=args.verbose)
    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir

    # Resolve symbols
    if args.all:
        symbols: Tuple[str, ...] = DEFAULT_SYMBOLS
    elif args.symbol:
        symbols = tuple(s.upper() for s in args.symbol)
    else:
        symbols = DEFAULT_SYMBOLS

    # Discover input files
    files = discover_inputs(symbols)
    files = [(s, d, p) for (s, d, p) in files if p.parent == input_dir]
    files = filter_by_date_range(files, args.start, args.end)
    if not files:
        log.warning(
            "no input files found for symbols=%s start=%s end=%s in %s",
            symbols,
            args.start,
            args.end,
            input_dir,
        )
        return 0
    log.info(
        "processing %d file(s) for symbols=%s start=%s end=%s",
        len(files),
        symbols,
        args.start,
        args.end,
    )
    total = 0
    for sym, date_str, in_path in files:
        out_path = output_dir / f"{sym.lower()}_{date_str}.jsonl"
        total += process_file(sym, date_str, in_path, out_path, log)
    log.info("done: %d total features written across %d file(s)", total, len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())

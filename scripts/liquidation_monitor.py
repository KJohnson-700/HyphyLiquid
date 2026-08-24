"""
HyphyLiquid - continuous liquidation monitor.

Reads the live trade files (written by collect_trades.py), runs each
new trade through LiquidationDetector, and logs detected events to
data/liquidations.jsonl.

Run foreground:
    .\\venv\\Scripts\\python.exe scripts\\liquidation_monitor.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.liquidation_detector import (
    LiquidationDetector,
    LiquidationEvent,
    TradeEvent,
)
from src.strategy.event_features import write_event_features

TRADE_DIR = PROJECT_ROOT / "data" / "trades"
LOG_PATH = PROJECT_ROOT / "data" / "liquidations.jsonl"
STATE_PATH = PROJECT_ROOT / "data" / ".liquidation_monitor_state.json"


def _state_path() -> Path:
    """Resolve the offset-state path at call time.

    Reads PROJECT_ROOT on each call instead of the import-time STATE_PATH
    constant, so tests that redirect PROJECT_ROOT into a temp dir don't
    write offsets into the real data/ directory.
    """
    return PROJECT_ROOT / "data" / ".liquidation_monitor_state.json"
POLL_INTERVAL_S = 5  # check for new trades every 5s
# Prune the state file (drop dead keys) every Nth scan. At POLL_INTERVAL_S=5s
# that's 60s, which is frequent enough to keep the file small without
# incurring I/O on every iteration. Tune up if prune work becomes visible.
PRUNE_EVERY_N_SCANS = 12


def _symbol_from_trade_path(path: Path) -> str:
    """Return canonical symbol from a trade jsonl filename.

    Trade files use a sanitized stem (xyz_gold, btc, eth, etc.) — not the
    on-wire HL symbol. The sanitize rule is: lowercase, replace ':' with '_'.
    We invert that for HIP-3 names here. Updated 2026-08-22 to support all
    11 HIP-3 research symbols; falls back to a leading-underscore split for
    anything else.
    """
    stem = path.stem
    # HIP-3 family (xyz: deployer). Match prefix and convert back to canonical.
    if stem.startswith("xyz_gold"):
        return "xyz:GOLD"
    if stem.startswith("xyz_silver"):
        return "xyz:SILVER"
    if stem.startswith("xyz_nvda"):
        return "xyz:NVDA"
    if stem.startswith("xyz_msft"):
        return "xyz:MSFT"
    if stem.startswith("xyz_sp500"):
        return "xyz:SP500"
    if stem.startswith("xyz_cl"):
        return "xyz:CL"
    if stem.startswith("xyz_mu"):
        return "xyz:MU"
    if stem.startswith("xyz_mstr"):
        return "xyz:MSTR"
    if stem.startswith("xyz_brentoil"):
        return "xyz:BRENTOIL"
    if stem.startswith("xyz_coin"):
        return "xyz:COIN"
    if stem.startswith("xyz_googl"):
        return "xyz:GOOGL"
    return stem.split("_")[0].upper()


def _last_line_offset(path: Path) -> int:
    """Return the byte offset where we left off reading this file."""
    state = PROJECT_ROOT / "data" / ".liquidation_monitor_state.json"
    if not state.exists():
        return 0
    try:
        state_data = json.loads(state.read_text(encoding="utf-8"))
        return int(state_data.get(str(path), 0))
    except Exception:
        return 0


def _save_offset(path: Path, offset: int) -> None:
    state_path = _state_path()
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    state[str(path)] = offset
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_state_for_prune() -> dict:
    """Read the state JSON. Returns empty dict on missing/corrupt."""
    state_path = _state_path()
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def prune_stale_state_keys() -> int:
    """Drop state keys whose file no longer exists on disk.

    Called every Nth scan from main(). Returns the number of keys dropped.
    Idempotent: safe to call repeatedly, no-op when nothing is stale.
    """
    state = _load_state_for_prune()
    if not state:
        return 0
    live = {k: v for k, v in state.items() if Path(k).exists()}
    dropped = len(state) - len(live)
    if dropped == 0:
        return 0
    # Atomic-ish write: write to a temp file, then rename. Avoids a partial
    # state file if the process is killed mid-write.
    state_path = _state_path()
    tmp = state_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(live, indent=2), encoding="utf-8")
        tmp.replace(state_path)
    except Exception as e:
        print(f"  [prune] write failed: {e}", flush=True)
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        return 0
    print(f"  [prune] dropped {dropped} stale key(s); state now {len(live)} live", flush=True)
    return dropped


def _ev_to_record(ev: LiquidationEvent) -> dict:
    return {
        "ts": datetime.fromtimestamp(ev.timestamp_ms / 1000, tz=timezone.utc).isoformat(),
        "symbol": ev.symbol,
        "side": ev.side,
        "total_notional": ev.total_notional,
        "n_fills": ev.n_fills,
        "price_avg": ev.price_avg,
        "duration_ms": ev.duration_ms,
        "confidence": ev.confidence,
        "reason": ev.reason,
    }


def _scan_once(
    detector: LiquidationDetector,
    seen_tids: set,
    trade_dir: Path = TRADE_DIR,
    log_path: Path = LOG_PATH,
) -> tuple[int, int]:
    """One pass over every trade file. Returns (new_trades, new_events).

    Skips blank lines and JSON-decode errors (partial flush from upstream
    writers) without raising. New trade events are appended to log_path.
    """
    total_new_trades = 0
    total_events = 0
    for path in sorted(trade_dir.glob("*.jsonl")):
        # Skip if the file no longer exists at the recorded path. This happens
        # when compress_old_data.py gzips an old file: the .jsonl is removed
        # and the .jsonl.gz remains. The state file's offset for the .jsonl
        # becomes meaningless. We treat missing files as "nothing to do" and
        # let the offset expire on the next run (the file path is keyed in
        # state, so it doesn't conflict with new files).
        if not path.exists():
            continue
        sym = _symbol_from_trade_path(path)
        offset = _last_line_offset(path)
        last_good_offset = offset
        with path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    last_good_offset = f.tell()
                    break
                if not line.strip():
                    last_good_offset = f.tell()
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    # A writer may be midway through appending the final line.
                    # Keep the offset before that line so the next scan retries it.
                    if not line.endswith("\n"):
                        print(
                            f"  [warn] partial json in {path.name}; retrying next scan",
                            flush=True,
                        )
                        last_good_offset = line_start
                        break
                    # Complete but malformed line: skip it and keep going.
                    print(
                        f"  [warn] skipping bad json in {path.name}: {e}",
                        flush=True,
                    )
                    last_good_offset = f.tell()
                    continue
                t = rec.get("trade", {})
                tid = t.get("tid")
                if tid is not None and str(tid) in seen_tids:
                    last_good_offset = f.tell()
                    continue
                if tid is not None:
                    seen_tids.add(str(tid))
                try:
                    ev_trade = TradeEvent(
                        symbol=sym,
                        timestamp_ms=int(t.get("time", 0)),
                        side=t.get("side", "?"),
                        price=float(t.get("px", 0)),
                        size=float(t.get("sz", 0)),
                        tid=tid,
                    )
                except Exception:
                    last_good_offset = f.tell()
                    continue
                new_events = detector.feed(ev_trade)
                total_new_trades += 1
                for ev in new_events:
                    rec_out = _ev_to_record(ev)
                    with log_path.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(rec_out) + "\n")
                    # Snapshot book + asset-ctx state at detection time
                    # and write to data/event_features.jsonl. This is the
                    # at-event feature store called for by the BTC/ETH
                    # strategy sweep (docs/2026-08-02-RESEARCH-btc-eth-
                    # hyperliquid-strategy-sweep.md, line 162).
                    # Live mode: pass nearest=False so we get the "now"
                    # snapshot (= the most recent l2book/asset_ctx).
                    try:
                        from src.strategy.event_features import snapshot_event_features
                        features = snapshot_event_features(rec_out, nearest=False)
                        with (PROJECT_ROOT / "data" / "event_features.jsonl").open("a", encoding="utf-8") as ff:
                            ff.write(json.dumps(features) + "\n")
                    except Exception as feat_err:
                        print(
                            f"  [warn] event_features snapshot failed: {feat_err}",
                            flush=True,
                        )
                    total_events += 1
                    print(
                        f"  [{rec_out['ts'][11:19]}] {ev.symbol:3}  {ev.side}  "
                        f"${ev.total_notional:>12,.0f}  {ev.n_fills:>2} fills  "
                        f"conf={ev.confidence:.2f}  ({ev.reason})",
                        flush=True,
                    )
                last_good_offset = f.tell()
        _save_offset(path, last_good_offset)
    return total_new_trades, total_events


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"Liquidation monitor started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Polling {TRADE_DIR} every {POLL_INTERVAL_S}s")
    print(f"Logging to {LOG_PATH}")
    print()

    detector = LiquidationDetector(per_symbol=True)
    seen_tids: set = set()

    scan_count = 0
    while True:
        scan_count += 1
        total_new_trades, total_events = _scan_once(detector, seen_tids)
        if total_new_trades:
            print(f"  [scan] {total_new_trades} new trades, {total_events} events", flush=True)
        # Periodic prune: drop state keys whose file was gzipped/rotated out.
        if scan_count % PRUNE_EVERY_N_SCANS == 0:
            prune_stale_state_keys()
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nLiquidation monitor stopped.")
        sys.exit(0)

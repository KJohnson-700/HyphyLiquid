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
POLL_INTERVAL_S = 5  # check for new trades every 5s


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
    state_path = PROJECT_ROOT / "data" / ".liquidation_monitor_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    state[str(path)] = offset
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


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
    for path in trade_dir.glob("*.jsonl"):
        sym = path.name.split("_")[0].upper()
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

    while True:
        total_new_trades, total_events = _scan_once(detector, seen_tids)
        if total_new_trades:
            print(f"  [scan] {total_new_trades} new trades, {total_events} events", flush=True)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nLiquidation monitor stopped.")
        sys.exit(0)

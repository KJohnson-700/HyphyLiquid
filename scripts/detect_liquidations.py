"""Run the liquidation detector over the trade history we already have."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.liquidation_detector import (
    LiquidationDetector,
    TradeEvent,
)

DATA_DIR = PROJECT_ROOT / "data" / "trades"


def main() -> int:
    detector = LiquidationDetector()
    total_trades = 0
    for path in sorted(DATA_DIR.glob("*.jsonl")):
        sym = path.name.split("_")[0].upper()
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if not line:
                continue
            rec = json.loads(line)
            t = rec.get("trade", {})
            try:
                ev = TradeEvent(
                    symbol=sym,
                    timestamp_ms=int(t.get("time", 0)),
                    side=t.get("side", "?"),
                    price=float(t.get("px", 0)),
                    size=float(t.get("sz", 0)),
                    tid=t.get("tid"),
                )
                detector.feed(ev)
                total_trades += 1
            except Exception as e:
                print(f"  skip trade: {e}")

    print(f"Scanned {total_trades} trades")
    print(f"Detected {len(detector.events)} probable liquidation events:")
    print()
    for ev in sorted(detector.events, key=lambda e: -e.total_notional):
        ts = ev.fills[0].timestamp_ms / 1000
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
        print(
            f"  {dt}  {ev.symbol:3}  {ev.side}  ${ev.total_notional:>12,.0f}  "
            f"{ev.n_fills:>2} fills  in {ev.duration_ms:>5}ms  "
            f"avg ${ev.price_avg:>10,.2f}  conf={ev.confidence:.2f}  "
            f"({ev.reason})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

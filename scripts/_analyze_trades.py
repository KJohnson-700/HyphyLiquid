"""Quick analysis: what are the largest trades? what does the size distribution look like?"""
import json
from datetime import datetime, timezone
from pathlib import Path

data_dir = Path(r"C:\Users\AbuBa\Desktop\HyphyLiquid\data\trades")
for path in sorted(data_dir.glob("*.jsonl")):
    sym = path.name.split("_")[0].upper()
    print(f"=== {sym} ===")
    trades = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        if not line:
            continue
        rec = json.loads(line)
        t = rec.get("trade", {})
        try:
            trades.append({
                "time": t.get("time", 0),
                "sz": float(t.get("sz", 0)),
                "px": float(t.get("px", 0)),
                "side": t.get("side", "?"),
                "tid": t.get("tid"),
                "notional": float(t.get("sz", 0)) * float(t.get("px", 0)),
            })
        except Exception:
            continue
    if not trades:
        continue
    print(f"  total trades: {len(trades)}")
    top = sorted(trades, key=lambda x: -x["notional"])[:10]
    print(f"  TOP 10 by notional:")
    for t in top:
        ts_str = datetime.fromtimestamp(t["time"]/1000, tz=timezone.utc).strftime("%H:%M:%S")
        print(f"    {ts_str} {t['side']} sz={t['sz']:>10.4f} px=${t['px']:>10,.2f} notional=${t['notional']:>12,.0f}")
    sizes = [t["notional"] for t in trades]
    sizes.sort()
    print(f"  notional: min=${sizes[0]:,.0f}  median=${sizes[len(sizes)//2]:,.0f}  p95=${sizes[int(len(sizes)*0.95)]:,.0f}  max=${sizes[-1]:,.0f}")
    print()

"""Show mainnet funding distribution percentiles + threshold counts."""
import pandas as pd

for sym in ("btc", "eth"):
    f = pd.read_csv(rf"C:\Users\AbuBa\Desktop\HyphyLiquid\data\{sym}_funding_90d_mainnet.csv")
    f["timestamp"] = pd.to_datetime(f["timestamp"], format="ISO8601", utc=True)
    r = f["funding_rate"]
    print(f"{sym.upper()}: n={len(r)}")
    print(f"  min={r.min()*100:.5f}%  p1={r.quantile(0.01)*100:.5f}%  p5={r.quantile(0.05)*100:.5f}%")
    print(f"  p50={r.median()*100:.5f}%  p95={r.quantile(0.95)*100:.5f}%  p99={r.quantile(0.99)*100:.5f}%  max={r.max()*100:.5f}%")
    for thr in [0.0001, 0.0003, 0.0005, 0.0010, 0.0020, 0.0030, 0.0050]:
        print(f"  count >  {thr*100:.4f}%: {(r > thr).sum()}")
    for thr in [-0.0001, -0.0003, -0.0005, -0.0010, -0.0020, -0.0030, -0.0050]:
        print(f"  count <  {thr*100:.4f}%: {(r < thr).sum()}")

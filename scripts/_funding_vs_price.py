"""
Funding vs price action on mainnet.

Hypothesis: high positive funding -> over-leveraged longs -> short squeeze cascade -> price drops.
Check: after a high funding event, what does price do over the next 1h, 4h, 24h?

This is a directional check, not a backtest. Just to see if there's a signal at all.
"""
import pandas as pd

DATA_DIR = r"C:\Users\AbuBa\Desktop\HyphyLiquid\data"

for sym in ("btc", "eth"):
    print(f"=== {sym.upper()} ===")
    f = pd.read_csv(f"{DATA_DIR}/{sym}_funding_90d_mainnet.csv")
    f["timestamp"] = pd.to_datetime(f["timestamp"], format="ISO8601", utc=True)
    c = pd.read_csv(f"{DATA_DIR}/{sym}_candles_1h_90d_mainnet.csv")
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)

    # Merge funding -> next 1h candle close, 4h, 24h
    # Funding events happen at top of hour; the 1h candle starting at that hour
    # is the "next bar"
    df = f[["timestamp", "funding_rate"]].merge(
        c[["timestamp", "close"]], on="timestamp", how="inner"
    ).sort_values("timestamp").reset_index(drop=True)

    # For each funding event, find the close at +1h, +4h, +24h
    closes = c.set_index("timestamp")["close"]
    for offset_label, offset_h in [("+1h", 1), ("+4h", 4), ("+24h", 24)]:
        rows = []
        for _, row in df.iterrows():
            t = row["timestamp"]
            entry = row["close"]
            target_ts = t + pd.Timedelta(hours=offset_h)
            if target_ts in closes.index:
                exit_ = closes.loc[target_ts]
                rows.append({"funding": row["funding_rate"], "ret": (exit_ - entry) / entry})
        if not rows:
            continue
        rdf = pd.DataFrame(rows)
        # Split by funding quintile (use rank to handle ties)
        rdf["funding_bucket"] = pd.qcut(rdf["funding"].rank(method="first"), 5, labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"])
        grp = rdf.groupby("funding_bucket", observed=True)["ret"].agg(["mean", "count"])
        grp["mean_bps"] = grp["mean"] * 10000
        grp["funding_rate_pct"] = rdf.groupby("funding_bucket", observed=True)["funding"].mean() * 100
        print(f"\n  Forward return {offset_label} by funding quintile:")
        print(grp.to_string())
    print()

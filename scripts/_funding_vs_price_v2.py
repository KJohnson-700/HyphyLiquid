"""
Funding vs price action — proper version using ALL events.

Truncate funding timestamps to the hour so they match candle timestamps
exactly. Funding events at HH:00:00.059 align with candle at HH:00:00.000
when truncated.
"""
import pandas as pd

DATA_DIR = r"C:\Users\AbuBa\Desktop\HyphyLiquid\data"

for sym in ("btc", "eth"):
    print(f"=== {sym.upper()} ===")
    f = pd.read_csv(f"{DATA_DIR}/{sym}_funding_90d_mainnet.csv")
    f["timestamp"] = pd.to_datetime(f["timestamp"], format="ISO8601", utc=True)
    f["hour_ts"] = f["timestamp"].dt.floor("h")  # truncate to hour
    c = pd.read_csv(f"{DATA_DIR}/{sym}_candles_1h_90d_mainnet.csv")
    c["timestamp"] = pd.to_datetime(c["timestamp"], format="ISO8601", utc=True)

    closes = c.set_index("timestamp")["close"]
    n_total = len(f)
    print(f"  total funding events: {n_total}")

    # For each funding event, compute forward return at +1h, +4h, +24h
    rows = []
    matched = 0
    for _, row in f.iterrows():
        t = row["hour_ts"]
        if t not in closes.index:
            continue
        matched += 1
        entry = closes.loc[t]
        for off in (1, 4, 24):
            tgt = t + pd.Timedelta(hours=off)
            if tgt in closes.index:
                rows.append({
                    "funding": row["funding_rate"],
                    "off": off,
                    "ret": (closes.loc[tgt] - entry) / entry,
                })
    df = pd.DataFrame(rows)
    print(f"  matched events: {matched} of {n_total} ({matched/n_total*100:.1f}%)")

    # Bin by fixed-rate thresholds
    edges = [-float("inf"), -1.5e-5, -1.0e-5, 0.0, 1.0e-5, 1.25e-5, 1.5e-5, float("inf")]
    labels = ["< -1.5e-5", "-1.5e-5..-1.0e-5", "-1.0e-5..0", "0..1.0e-5",
              "1.0e-5..1.25e-5", "1.25e-5..1.5e-5", "> 1.5e-5"]
    df["bucket"] = pd.cut(df["funding"], bins=edges, labels=labels)

    pivot = df.pivot_table(
        index="bucket", columns="off", values="ret", aggfunc="mean", observed=True
    ) * 10000
    counts = df.pivot_table(index="bucket", columns="off", values="ret", aggfunc="count", observed=True)
    print("\n  Mean forward return (bps) by funding bucket:")
    print(pivot.round(2).to_string())
    print("\n  N events per bucket:")
    print(counts.to_string())

    n_pos = (f["funding_rate"] > 0).sum()
    n_neg = (f["funding_rate"] < 0).sum()
    n_zero = (f["funding_rate"] == 0).sum()
    print(f"\n  funding sign: pos={n_pos} ({n_pos/n_total*100:.1f}%)  "
          f"neg={n_neg} ({n_neg/n_total*100:.1f}%)  zero={n_zero}")
    print()

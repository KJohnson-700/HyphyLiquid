"""Scan the HL SDK for liquidation-related endpoints."""
import hyperliquid
import os, glob

sdk_path = os.path.dirname(hyperliquid.__file__)
print(f"SDK path: {sdk_path}")
for f in glob.glob(f"{sdk_path}/**/*.py", recursive=True):
    try:
        content = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    low = content.lower()
    if "liquidat" in low or "liq" in low.split():
        print(f"  match: {f}")

# Also dump the full list of post() request types from the SDK
print("\n=== Looking for endpoint request types ===")
for f in glob.glob(f"{sdk_path}/**/*.py", recursive=True):
    try:
        content = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    # crude: look for string literals used as request types
    import re
    matches = set(re.findall(r'"([a-z][a-zA-Z]+)"', content))
    for kw in ("liquidatedPositions", "userFills", "trades", "candle", "fundingHistory"):
        if kw in matches:
            print(f"  {kw} found in {f}")

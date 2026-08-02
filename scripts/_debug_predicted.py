"""Debug predictedFundings response shape."""
import requests
r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "predictedFundings"}, timeout=15)
data = r.json()
print(f"top-level: {type(data).__name__}, len={len(data)}")
print(f"first 3 items: {data[:3]}")
print(f"item types: {[type(x).__name__ for x in data[:5]]}")
if isinstance(data[0], list):
    print(f"data[0]: {data[0]}")
    print(f"data[0] type: {type(data[0][0]).__name__ if data[0] else 'empty'}")

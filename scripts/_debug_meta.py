"""Debug metaAndAssetCtxs response shape."""
import requests
r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
meta = r.json()
print(f"top type: {type(meta).__name__}")
if hasattr(meta, "__len__"):
    print(f"len: {len(meta)}")
if isinstance(meta, list):
    print(f"meta[0] type: {type(meta[0]).__name__}, len: {len(meta[0]) if isinstance(meta[0], list) else 'N/A'}")
    if isinstance(meta[0], list) and meta[0]:
        print(f"meta[0][0] type: {type(meta[0][0]).__name__}, sample: {str(meta[0][0])[:200]}")
    print(f"meta[1] type: {type(meta[1]).__name__}, len: {len(meta[1]) if isinstance(meta[1], list) else 'N/A'}")
    if isinstance(meta[1], list) and meta[1]:
        print(f"meta[1][0] type: {type(meta[1][0]).__name__}")
        if isinstance(meta[1][0], dict):
            print(f"meta[1][0] keys: {list(meta[1][0].keys())[:15]}")
            print(f"meta[1][0]: {str(meta[1][0])[:400]}")

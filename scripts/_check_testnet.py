"""Quick check that testnet is reachable."""
import sys
sys.path.insert(0, r"C:\Users\AbuBa\Desktop\HyphyLiquid")
from hyperliquid.info import Info
info = Info("https://api.hyperliquid-testnet.xyz", skip_ws=True)
print(f"Testnet perps: {len(info.meta().get('universe', []))}")
print(f"BTC testnet mid: ${float(info.all_mids().get('BTC', 0)):,.2f}")

"""Probe Hyperliquid's official S3 archive for public liquidation data."""
import subprocess
import sys
from pathlib import Path

# Check what tools are available
for tool in ("aws", "lz4"):
    r = subprocess.run([tool, "--version"], capture_output=True, text=True)
    if r.returncode == 0:
        print(f"{tool}: {r.stdout.strip()[:200]}")
    else:
        print(f"{tool}: NOT INSTALLED ({r.stderr.strip()[:200]})")

print()
print("=== Try to list S3 bucket anonymously ===")
# Try anonymous S3 listing
r = subprocess.run(
    ["aws", "s3", "ls", "s3://hyperliquid-archive/", "--no-sign-request"],
    capture_output=True, text=True,
)
print(f"exit={r.returncode}")
print(f"stdout: {r.stdout[:1000]}")
print(f"stderr: {r.stderr[:500]}")

print()
print("=== Try one file (L2 book snapshot) ===")
r = subprocess.run(
    ["aws", "s3", "cp",
     "s3://hyperliquid-archive/market_data/20260101/0/l2Book/BTC.lz4",
     "/tmp/BTC_test.lz4",
     "--no-sign-request"],
    capture_output=True, text=True,
)
print(f"exit={r.returncode}")
print(f"stdout: {r.stdout[:500]}")
print(f"stderr: {r.stderr[:500]}")

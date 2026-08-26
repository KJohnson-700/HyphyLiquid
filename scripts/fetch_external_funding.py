"""
fetch_external_funding.py - Cross-venue funding rate fetcher.

Pulls current funding rates from Binance Futures (public, no key needed)
and appends them to data/external_funding.jsonl. The dashboard reads
this file to show "vs Binance" divergence in the funding table.

Binance endpoint: GET https://fapi.binance.com/fapi/v1/premiumIndex
  Returns: {symbol, markPrice, indexPrice, lastFundingRate, nextFundingTime, ...}

For HIP-3 names (xyz:GOLD, etc.), no cross-venue comparison is possible
(no equivalent on Binance). These are recorded as null for the cross-ref.

Run standalone:
  python scripts/fetch_external_funding.py
  python scripts/fetch_external_funding.py --once --quiet

Wired into the funding-arb signal as a confidence boost (later commit).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA = PROJECT_ROOT / "data"
EXT_FUNDING_PATH = DATA / "external_funding.jsonl"

# Map our symbol -> Binance symbol AND Binance's funding interval in hours
# (default is 8h, but some pairs are 1h, 2h, or 4h).
SYMBOL_MAP = {
    "BTC":      ("BTCUSDT",  8),
    "ETH":      ("ETHUSDT",  8),
    "HYPE":     (None,       None),  # not on Binance
    "SOL":      ("SOLUSDT",  8),
    "xyz:GOLD":     (None, None),
    "xyz:SILVER":   (None, None),
    "xyz:NVDA":     (None, None),
    "xyz:MSFT":     (None, None),
    "xyz:SP500":    (None, None),
    "xyz:CL":       ("CLUSDT",  8),  # crude oil futures
    "xyz:MU":       (None, None),
    "xyz:MSTR":     (None, None),
    "xyz:BRENTOIL": (None, None),
    "xyz:COIN":     (None, None),
    "xyz:GOOGL":    (None, None),
}

BINANCE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
TIMEOUT_S = 10


def fetch_binance_funding() -> dict[str, dict]:
    """Returns {binance_symbol: {mark, funding_rate, next_funding_ts}}."""
    try:
        r = requests.get(BINANCE_URL, timeout=TIMEOUT_S)
        r.raise_for_status()
        arr = r.json()
    except Exception as e:
        print(f"  binance fetch failed: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for row in arr:
        sym = row.get("symbol")
        if not sym:
            continue
        try:
            out[sym] = {
                "mark": float(row.get("markPrice") or 0),
                "funding_rate": float(row.get("lastFundingRate") or 0),
                "next_funding_ts": int(row.get("nextFundingTime") or 0),
            }
        except (TypeError, ValueError):
            continue
    return out


def build_record() -> dict:
    """Build a single cross-venue snapshot for our universe.

    Binance reports funding per-event. To compare with HL's per-hour funding,
    we normalize: binance_funding_per_hour = binance_funding / binance_interval_h.
    """
    binance = fetch_binance_funding()
    ts = datetime.now(timezone.utc).isoformat()
    per_symbol: dict[str, dict] = {}
    for our_sym, (bn_sym, bn_interval_h) in SYMBOL_MAP.items():
        if bn_sym is None:
            per_symbol[our_sym] = {
                "binance_sym": None,
                "binance_funding": None,
                "binance_funding_per_hour": None,
                "binance_mark": None,
            }
            continue
        b = binance.get(bn_sym, {})
        bn_fund = b.get("funding_rate")
        bn_fund_per_hour = (bn_fund / bn_interval_h) if (bn_fund is not None and bn_interval_h) else None
        per_symbol[our_sym] = {
            "binance_sym": bn_sym,
            "binance_funding": bn_fund,
            "binance_funding_per_hour": bn_fund_per_hour,
            "binance_mark": b.get("mark"),
        }
    return {
        "ts_utc": ts,
        "binance_count": sum(1 for v in per_symbol.values() if v.get("binance_funding") is not None),
        "symbols": per_symbol,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-venue funding fetcher (Binance)")
    ap.add_argument("--once", action="store_true", help="fetch once and exit (default: loop)")
    ap.add_argument("--interval", type=int, default=60, help="seconds between fetches in loop mode (default 60)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-iteration logging")
    args = ap.parse_args()

    EXT_FUNDING_PATH.parent.mkdir(parents=True, exist_ok=True)

    while True:
        rec = build_record()
        with EXT_FUNDING_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        if not args.quiet:
            n = rec["binance_count"]
            print(f"  {rec['ts_utc']}  binance_funding_count={n}/4  appended to {EXT_FUNDING_PATH.name}")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

"""
binance_delta.py - shared helper for cross-venue funding delta.

Reads data/external_funding.jsonl (written by fetch_external_funding.py)
and returns a per-symbol delta series normalized to per-hour.

Returns: dict[symbol, dict] with:
  hl_funding: float      # latest hourly funding on HL (from asset_ctx)
  binance_funding: float # latest hourly funding on Binance
  delta: float          # hl_funding - binance_funding (per hour)
  binance_sym: str|None # "BTCUSDT" etc., None if no cross-venue mapping
  fetched_ts: str       # when the Binance record was fetched
  age_seconds: float    # how stale this record is
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys_path_added = False
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
    sys_path_added = True

EXT_FUNDING_PATH = PROJECT_ROOT / "data" / "external_funding.jsonl"

# How stale can the Binance record be before we ignore it?
# 60 minutes is fine for funding signals (the strategy runs hourly anyway;
# a 1h-stale Binance record is still relevant). Tighter than this just
# causes the dashboard to show "[dim]—[/]" most of the time.
MAX_AGE_S = 3600  # 60 minutes

# Maps our symbol -> Binance symbol AND Binance's funding interval in hours
SYMBOL_MAP: dict[str, tuple[str | None, int | None]] = {
    "BTC":          ("BTCUSDT",  8),
    "ETH":          ("ETHUSDT",  8),
    "HYPE":         (None,       None),
    "SOL":          ("SOLUSDT",  8),
    "xyz:GOLD":     (None, None),
    "xyz:SILVER":   (None, None),
    "xyz:NVDA":     (None, None),
    "xyz:MSFT":     (None, None),
    "xyz:SP500":    (None, None),
    "xyz:CL":       ("CLUSDT",  8),
    "xyz:MU":       (None, None),
    "xyz:MSTR":     (None, None),
    "xyz:BRENTOIL": (None, None),
    "xyz:COIN":     (None, None),
    "xyz:GOOGL":    (None, None),
}


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def load_latest_binance_funding() -> dict[str, dict[str, Any]]:
    """Read the most recent line of external_funding.jsonl.

    Returns: dict[symbol, {binance_sym, binance_funding_per_hour, binance_mark, fetched_ts, age_seconds}]
    Empty dict if no record exists or all are stale.
    """
    if not EXT_FUNDING_PATH.exists():
        return {}
    last_rec: dict | None = None
    try:
        with EXT_FUNDING_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    last_rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {}
    if not last_rec:
        return {}
    fetched_ts = _parse_iso(last_rec.get("ts_utc"))
    if fetched_ts is None:
        return {}
    age_s = (datetime.now(timezone.utc) - fetched_ts).total_seconds()
    if age_s > MAX_AGE_S:
        return {}  # too stale to be useful

    out: dict[str, dict[str, Any]] = {}
    for sym, (bn_sym, bn_interval_h) in SYMBOL_MAP.items():
        per = last_rec.get("symbols", {}).get(sym, {})
        out[sym] = {
            "binance_sym": bn_sym,
            "binance_funding_per_hour": per.get("binance_funding_per_hour"),
            "binance_mark": per.get("binance_mark"),
            "fetched_ts": last_rec.get("ts_utc"),
            "age_seconds": age_s,
        }
    return out


def compute_delta(our_sym: str, hl_funding: float | None) -> dict[str, Any]:
    """For a single symbol: combine HL funding (per hour) with Binance data.

    Returns dict with binance_funding_per_hour, delta (hl - binance), confidence.
    """
    latest = load_latest_binance_funding()
    sym_data = latest.get(our_sym, {})
    bn_fund = sym_data.get("binance_funding_per_hour")
    if hl_funding is None or bn_fund is None:
        return {
            "binance_funding_per_hour": bn_fund,
            "delta": None,
            "binance_sym": sym_data.get("binance_sym"),
            "fetched_ts": sym_data.get("fetched_ts"),
            "age_seconds": sym_data.get("age_seconds"),
            "boost_eligible": False,
        }
    delta = hl_funding - bn_fund
    return {
        "binance_funding_per_hour": bn_fund,
        "delta": delta,
        "binance_sym": sym_data.get("binance_sym"),
        "fetched_ts": sym_data.get("fetched_ts"),
        "age_seconds": sym_data.get("age_seconds"),
        "boost_eligible": abs(delta) > 0.0001,  # 1bps/hour = 0.01%/hr
    }

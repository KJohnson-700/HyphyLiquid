"""Label closed trades with the market regime at entry.

Gate 2 of the graduation ladder requires at least 2 distinct regimes per lane,
so a lane cannot reach canary live off one lucky trend or chop window. Closed
trades carry no regime today, which blocks every asset regardless of PF.

Labels come from src.strategy.regime.classify_candle_regime, which uses only
candles strictly before the entry bar — no lookahead.

Writes a sidecar map (trade id -> regime) rather than mutating the positions
file, so the source of truth stays append-only and labelling stays auditable
and re-runnable.

Usage:
  python3 scripts/label_trade_regimes.py
  python3 scripts/label_trade_regimes.py --positions PATH --out PATH
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.regime import classify_candle_regime  # noqa: E402

DEFAULT_POSITIONS = PROJECT_ROOT / "data" / "paper_funding_neg_fade_positions.jsonl"
DEFAULT_OUT = PROJECT_ROOT / "data" / "trade_regimes.json"
DATA_DIR = PROJECT_ROOT / "data"


def _candle_file(symbol: str) -> Path | None:
    """Historical 1h candles fetched by fetch_historical.py, if present."""
    stem = symbol.lower().replace(":", "_")
    for days in (90, 30):
        for env in ("mainnet", "testnet"):
            p = DATA_DIR / f"{stem}_candles_1h_{days}d_{env}.csv"
            if p.exists():
                return p
    return None


def load_candles(symbol: str) -> list[dict] | None:
    p = _candle_file(symbol)
    if p is None:
        return None
    df = pd.read_csv(p, parse_dates=["timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    df = df.sort_values("timestamp").reset_index(drop=True)
    # regime.py / lane_backtest.py read WS candle keys (o/h/l/c/v), not the
    # CSV's open/high/low/close. Without this mapping every lookup returns
    # None and the classifier reports "no_data" for every trade.
    return [
        {"timestamp": r.timestamp, "t": r.timestamp,
         "o": float(r.open), "h": float(r.high),
         "l": float(r.low), "c": float(r.close), "v": float(r.volume)}
        for r in df.itertuples(index=False)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", type=Path, default=DEFAULT_POSITIONS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not args.positions.exists():
        print(f"ERROR: {args.positions} not found", file=sys.stderr)
        return 1

    rows = []
    for line in args.positions.open():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    closed = [r for r in rows if r.get("status") == "closed"]

    cache: dict[str, list[dict] | None] = {}
    labels: dict[str, dict] = {}
    counts: dict[str, dict[str, int]] = {}
    unlabelled = 0

    for r in closed:
        sym = r.get("symbol", "?")
        tid = r.get("paper_id") or r.get("decision_id")
        if not tid:
            continue
        if sym not in cache:
            cache[sym] = load_candles(sym)
        candles = cache[sym]
        if not candles:
            unlabelled += 1
            continue
        entry = pd.Timestamp(r["entry_ts"])
        idx = next((i for i, c in enumerate(candles) if c["timestamp"] >= entry), None)
        if idx is None or idx <= 0:
            unlabelled += 1
            continue
        reg = classify_candle_regime(candles, idx)
        labels[tid] = {"symbol": sym, "entry_ts": r["entry_ts"],
                       "regime": reg.label, "trend": reg.trend,
                       "band_width_bucket": reg.band_width_bucket}
        counts.setdefault(sym, {}).setdefault(reg.label, 0)
        counts[sym][reg.label] += 1

    args.out.write_text(json.dumps(labels, indent=2, sort_keys=True))
    print(f"labelled {len(labels)}/{len(closed)} closed trades "
          f"({unlabelled} without candle coverage)")
    print(f"wrote {args.out}\n")
    for sym in sorted(counts):
        dist = ", ".join(f"{k}={v}" for k, v in sorted(counts[sym].items()))
        n_reg = len(counts[sym])
        flag = "ok " if n_reg >= 2 else "LOW"
        print(f"  [{flag}] {sym:6} {n_reg} regime(s): {dist}")
    low = [s for s in counts if len(counts[s]) < 2]
    if low:
        print(f"\n  under 2 regimes, cannot reach canary: {', '.join(sorted(low))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

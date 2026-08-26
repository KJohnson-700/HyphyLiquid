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

from src.strategy.regime import atr_at, classify_candle_regime  # noqa: E402

_atr_thresh_cache: dict[str, float] = {}


def high_atr_threshold(symbol: str, candles: list[dict], pct: float = 80.0) -> float:
    """Per-asset "high volatility" threshold, as a percentile of its own ATR.

    classify_candle_regime defaults to an absolute atr_pct >= 0.50, which
    overrides every other label. Measured on the 7-month panel that makes the
    label meaningless for volatile assets: 100% of HYPE bars and 100% of ZEC
    bars classify as high_vol_cascade regardless of anything happening, against
    83% for ETH and 65% for BTC. A gate requiring two regimes was therefore
    unpassable for HYPE for reasons that have nothing to do with its strategy.

    Using the asset's own 80th percentile restores the intent -- unusually
    volatile *for this asset* -- and lets the other labels appear at all.
    """
    if symbol in _atr_thresh_cache:
        return _atr_thresh_cache[symbol]
    vals = []
    for i in range(30, len(candles), 3):
        try:
            a = atr_at(candles, i, period=14)
            c = float(candles[i].get("c") or 0)
            if a and c > 0:
                vals.append(a / c * 100.0)
        except Exception:
            continue
    if len(vals) < 50:
        thr = 0.50                     # not enough history; keep the old default
    else:
        vals.sort()
        thr = vals[int(len(vals) * pct / 100.0)]
    _atr_thresh_cache[symbol] = thr
    return thr

DEFAULT_POSITIONS = PROJECT_ROOT / "data" / "paper_funding_neg_fade_positions.jsonl"
SWING_POSITIONS = PROJECT_ROOT / "data" / "paper_swing_positions.jsonl"
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


_PANEL = DATA_DIR / "candle_panel.csv"
_panel_cache: dict | None = None


def _from_panel(symbol: str):
    """Hourly candles for one symbol out of candle_panel.csv.

    Preferred over the per-symbol *_candles_1h_90d_*.csv files, which come from
    fetch_historical and only ever covered a handful of symbols for 90 days.
    The panel is venue-sourced and now spans ~7 months for every symbol we
    trade, so preferring it is the difference between labelling 59 of 182
    trades and labelling nearly all of them -- and unlabelled trades were
    counting as a "no_data" regime that satisfied a promotion gate.
    """
    global _panel_cache
    if _panel_cache is None:
        if not _PANEL.exists():
            _panel_cache = {}
        else:
            df = pd.read_csv(_PANEL)
            df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
            df["ts"] = df["ts"].dt.tz_localize(None)
            _panel_cache = {sym: g.sort_values("ts").reset_index(drop=True)
                            for sym, g in df.groupby("symbol")}
    g = _panel_cache.get(symbol)
    if g is None or g.empty:
        return None
    return g.rename(columns={"ts": "timestamp"})


def load_candles(symbol: str) -> list[dict] | None:
    df = _from_panel(symbol)
    if df is None:
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
    # Every lane's positions, not just the fade lane's. A lane whose trades go
    # unlabelled shows "regimes: none labelled" and can never clear Gate 2's
    # diversity rule -- it would be blocked by a gap in our own tooling rather
    # than by anything about the strategy.
    ap.add_argument("--positions", type=Path, action="append", default=None,
                    help="repeatable; defaults to every known lane's file")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    paths = args.positions or [DEFAULT_POSITIONS, SWING_POSITIONS]
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("ERROR: no positions files found", file=sys.stderr)
        return 1

    rows = []
    lines = []
    for _p in paths:
        lines.extend(_p.read_text().splitlines())
    for line in lines:
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
        reg = classify_candle_regime(
            candles, idx,
            high_atr_pct=high_atr_threshold(sym, candles))
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

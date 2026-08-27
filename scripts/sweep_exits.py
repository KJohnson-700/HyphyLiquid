"""Do trailing exits rescue anything a fixed stop/TP killed?

Every strategy in this project was tested with one exit style: fixed stop,
fixed target, max hold. That is a real gap in variant coverage, and it matters
because the measured shape of these trades says the exit is where the money
leaks -- across HL listings the median favourable excursion was +31.1% against
a -12.0% adverse one, while the median 7-day HOLD return was -5.3%. The move
arrives and is given back. A fixed target either caps it or never fires.

Four exit styles, same entry signal, same costs, same both-halves standard:

  fixed        stop / target / timeout          (the current baseline)
  trail        stop ratchets up behind the high water mark
  be_trail     stop to breakeven at +1R, then trail
  partial      half off at +1R, rest trails

Nothing here changes an entry rule or a threshold. If a dead strategy comes
alive on a better exit, that is a genuine finding about exits. If none do, the
fixed-exit verdicts stand and the variant gap is closed.

Usage:
  python3 scripts/sweep_exits.py
  python3 scripts/sweep_exits.py --symbols ETH,HYPE,ZEC
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT)); sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from strategy_search import load_hl_with_funding, signal_funding_neg_fade  # noqa: E402
from paper_funding_neg_fade import NEG_THRESHOLD  # noqa: E402

COST = 0.08
SPLIT = pd.Timestamp("2026-05-01")


def pf(v):
    w = sum(x for x in v if x > 0); l = -sum(x for x in v if x < 0)
    return w / l if l > 0 else (float("inf") if w else 0.0)


def run(df, sig, stop, tp_mult, hold, style):
    """One long-only pass. `stop` is fractional; R = stop."""
    c = df["close"].values
    hi = df["high"].values if "high" in df else c
    lo = df["low"].values if "low" in df else c
    idx = df.index
    v = sig.to_numpy() if hasattr(sig, "to_numpy") else sig
    out, i, n = [], 1, len(df)
    while i < n - 1:
        if not (v[i] and not v[i - 1] and c[i] > 0):
            i += 1; continue
        e = float(c[i])
        sl = e * (1 - stop)
        tgt = e * (1 + stop * tp_mult)
        peak = e
        booked = 0.0          # realised portion, in % of entry
        frac = 1.0            # remaining position
        ex_i = ex_px = None
        for j in range(i + 1, min(i + 1 + hold, n)):
            h, l = float(hi[j]), float(lo[j])
            # stop first: within a bar we cannot know the order, so assume the
            # adverse touch happened first. Optimistic assumptions are how
            # backtests lie.
            if l <= sl:
                ex_i, ex_px = j, sl; break
            peak = max(peak, h)
            if style in ("trail", "be_trail", "partial"):
                if style == "be_trail" and peak >= e * (1 + stop) and sl < e:
                    sl = e                                   # breakeven at +1R
                if style == "partial" and frac == 1.0 and h >= e * (1 + stop):
                    booked += stop * 100 * 0.5               # half off at +1R
                    frac = 0.5
                    sl = max(sl, e)
                trail = peak * (1 - stop)
                if trail > sl:
                    sl = trail                               # ratchet only
            elif h >= tgt:
                ex_i, ex_px = j, tgt; break
        if ex_i is None:
            k = min(i + hold, n - 1); ex_i, ex_px = k, float(c[k])
        ret = booked + frac * ((ex_px - e) / e * 100.0) - COST
        out.append({"net_pct": ret, "entry_ts": str(idx[i])})
        i = ex_i + 1
    return out


def halves(tr):
    def t(x):
        v = pd.Timestamp(x["entry_ts"]); return v.tz_localize(None) if v.tzinfo else v
    return [x for x in tr if t(x) < SPLIT], [x for x in tr if t(x) >= SPLIT]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--min-half", type=int, default=20)
    args = ap.parse_args()

    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        p = pd.read_csv("data/candle_panel.csv"); f = pd.read_csv("data/funding_panel.csv")
        syms = sorted(set(p.symbol) & set(f.symbol))
    print(f"funding signal, 24h max hold, 4 exit styles, both halves must clear 1.5\n")
    print(f"{'symbol':13}{'style':10}{'stop':>6}{'n':>5}{'PF':>7}{'H1':>7}{'H2':>7}{'net%':>9}")
    winners = []
    for s in syms:
        try:
            r = load_hl_with_funding(s); df = r[0] if isinstance(r, tuple) else r
        except Exception:
            continue
        if len(df) < 2000:
            continue
        sig = signal_funding_neg_fade(df, funding_col="funding_actual",
                                      neg_threshold=NEG_THRESHOLD)
        for style in ("fixed", "trail", "be_trail", "partial"):
            best = None
            for stop in (0.02, 0.03, 0.04, 0.06):
                tr = run(df, sig, stop, 1.35, 24, style)
                h1, h2 = halves(tr)
                if len(h1) < args.min_half or len(h2) < args.min_half:
                    continue
                p1, p2 = pf([x["net_pct"] for x in h1]), pf([x["net_pct"] for x in h2])
                if p1 < 1.5 or p2 < 1.5:
                    continue
                vv = [x["net_pct"] for x in tr]
                cand = (pf(vv), stop, len(vv), p1, p2, sum(vv))
                if best is None or cand[0] > best[0]:
                    best = cand
            if best:
                p, stop, n, p1, p2, net = best
                print(f"{s:13}{style:10}{stop:>5.0%}{n:>5}{p:>7.2f}{p1:>7.2f}{p2:>7.2f}{net:>+8.1f}%")
                winners.append((s, style, p, n))
    print()
    by_style = {}
    for s, style, p, n in winners:
        by_style.setdefault(style, []).append((s, p))
    for style in ("fixed", "trail", "be_trail", "partial"):
        w = by_style.get(style, [])
        print(f"  {style:10} clears in {len(w)} symbol(s): "
              f"{', '.join(f'{s} {p:.2f}' for s, p in w) if w else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

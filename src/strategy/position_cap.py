"""Portfolio-level position cap, applied the way live execution would apply it.

The fade paper simulator walks each symbol independently, so nothing stops four
lanes holding at once. RiskManager.check_trade rejects any entry once
max_open_positions are already open (REJECTED_MAX_POSITIONS), and the observed
paper record had four lanes open for 11 hours. Every metric scored off that
record therefore counts trades live would never have taken.

This replays the trades in entry order against the same cap so the scorecard
sees the portfolio that live would actually have held.

Admission is first-come-first-served by entry time, which matches live: the
engine cannot know a better trade is coming an hour later. A blocked trade is
annotated rather than deleted, so the cost of the cap stays auditable.
"""
from __future__ import annotations

from typing import Any, Iterable

BLOCKED_REASON = "max_open_positions"


def apply_position_cap(
    trades: Iterable[dict[str, Any]],
    max_open: int,
    *,
    entry_key: str = "entry_ts",
    exit_key: str = "exit_ts",
) -> list[dict[str, Any]]:
    """Annotate each trade with ``admitted`` and, when blocked, ``blocked_by``.

    Returns a new list in entry order; input dicts are not mutated. Trades
    missing either timestamp are admitted untouched rather than silently
    dropped -- losing a trade is a worse failure than over-counting one, and
    it would be invisible in the totals.
    """
    if max_open < 1:
        raise ValueError(f"max_open must be >= 1, got {max_open}")

    usable, passthrough = [], []
    for t in trades:
        (usable if t.get(entry_key) and t.get(exit_key) else passthrough).append(t)

    usable.sort(key=lambda t: (str(t[entry_key]), str(t.get("symbol", ""))))

    open_exits: list[str] = []
    out: list[dict[str, Any]] = []
    for t in usable:
        entry = str(t[entry_key])
        # Release anything that closed at or before this entry. A position that
        # exits exactly when another enters frees its slot -- live would too.
        open_exits = [x for x in open_exits if x > entry]
        rec = dict(t)
        if len(open_exits) >= max_open:
            rec["admitted"] = False
            rec["blocked_by"] = BLOCKED_REASON
        else:
            rec["admitted"] = True
            open_exits.append(str(t[exit_key]))
        out.append(rec)

    for t in passthrough:
        rec = dict(t)
        rec["admitted"] = True
        out.append(rec)
    return out


def cap_summary(annotated: Iterable[dict[str, Any]], pnl_key: str = "net_pnl_usd") -> dict:
    """What the cap cost, so the change is never invisible."""
    rows = list(annotated)
    blocked = [t for t in rows if not t.get("admitted", True)]
    def _pnl(ts):
        return sum(float(t.get(pnl_key) or 0.0) for t in ts)
    return {
        "n_total": len(rows),
        "n_admitted": len(rows) - len(blocked),
        "n_blocked": len(blocked),
        "pnl_all": _pnl(rows),
        "pnl_admitted": _pnl(t for t in rows if t.get("admitted", True)),
        "pnl_blocked": _pnl(blocked),
        "blocked_symbols": sorted({t.get("symbol", "?") for t in blocked}),
    }

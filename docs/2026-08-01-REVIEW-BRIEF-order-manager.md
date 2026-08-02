---
project: HyphyLiquid
review_target: src/execution/order_manager.py + tests
reviewer: Codex (or any second-opinion AI)
date: 2026-08-01
status: pre-live-trading
---

# Codex Review Brief: OrderManager

## What I built

`src/execution/order_manager.py` (~300 lines) takes a `CascadeSignal` from the strategy layer, sizes the position, runs the risk check, and places the **entry + take-profit + stop-loss as a single atomic action** on Hyperliquid.

```python
mgr = OrderManager.from_env()              # loads .env, validates key+addr
result = mgr.execute(signal, candles, current_price=...)
# OrderResult: filled, error, oids, requested_size_usd, requested_leverage,
#              entry_px, tp_px, sl_px, risk_verdict
```

The atomic placement is via `exchange.bulk_orders(requests, grouping="positionTpsl")` — if the entry fails, neither TP nor SL exist. If entry fills, both TP and SL are live in the same block.

## Files to review

| File | What | Lines |
|---|---|---|
| `src/execution/order_manager.py` | The implementation | ~300 |
| `tests/test_order_manager.py` | 7 tests using a `FakeExchange` (no network) | ~180 |
| `src/risk.py` | What `RiskManager.check_trade()` returns | read for context |
| `src/strategy/cascade.py` | What `CascadeSignal` looks like | read for context |
| `AGENTS.md` §5 | The hard rules the order manager must respect | read for context |

## What I want you to check

Be skeptical. The code passes 7 unit tests and 98/98 total project tests, but the **live testnet trade has not been run yet** (waiting for user to manually trigger).

### 1. Sizing math
- 1% risk per trade = `bankroll * 0.01` = $10 for $1000 bankroll
- Notional = `risk / (sl_distance / entry)` = $10 / 0.0033 = ~$3000
- Leverage = notional / bankroll = ~3x
- All capped at `max_leverage * bankroll`
- **Question:** is the "capped at max_leverage" math right? If risk_pct=0.5 (huge) and max_leverage=2.0, do we end up at 2x or 50%? Walk through `test_size_capped_at_max_leverage`.

### 2. Risk integration
- Uses `RiskManager.check_trade(symbol, side, size_usd, leverage, stop_distance_usd)`. The order manager passes the *stop distance in USD* (notional × sl_pct), which is the worst-case loss if SL hits.
- **Question:** is the `stop_distance_usd` calculation right? In my code: `notional * (sl_distance / entry)`. The risk check divides that by bankroll to get risk_pct. Walk through with concrete numbers (bankroll=1000, entry=60000, sl=300, notional=3000).

### 3. Atomic order placement
- I send 3 orders in one `bulk_orders` call with `grouping="positionTpsl"`. Per HL docs, this means: "if entry fills, both TP and SL are active as a group. If entry fails, neither TP nor SL exist."
- The TP and SL are `{"trigger": {"triggerPx": X, "isMarket": True, "tpsl": "tp"|"sl"}}` with `reduce_only=True`.
- **Question:** is the `grouping="positionTpsl"` the right choice vs `"normalTpsl"`? Are the trigger orders correctly formed? Will the TP and SL be visible as "linked" to the entry on the HL UI?

### 4. Tick rounding and size rounding
- BTC tick = $1 (hardcoded in `_round_to_tick`)
- BTC size decimals = 5 (hardcoded in `_round_size`)
- ETH tick = $0.1, decimals = 4
- SOL tick = $0.01, decimals = 2
- Fallback: tick=$0.01, decimals=3 for unknown symbols
- **Question:** are these right? HL has a `meta()` call that returns per-asset `szDecimals`. Should the order manager use that instead of hardcoding? What's the cost of being wrong (rejected orders, surprise rounding)?

### 5. ATR fallback
- If candles are empty or have <15 bars, ATR = 0 → fallback `current_price * 0.005` (0.5%)
- This means a fresh install with no data history would trade on a 0.5% ATR basis
- **Question:** is 0.5% a sane fallback? Should we refuse to trade without proper ATR instead?

### 6. Edge cases I'm worried about

a. **What if the signal comes in at a price gap from the last candle close?** I use `current_price` if provided, else last candle close. If the order fills at a price different from what I computed, my SL/TP placement is now wrong relative to my actual entry. (Mitigation: I round entry to tick; but the *fill* price could be off by more than a tick if it's a market order.)

b. **What if HL rejects the trigger order but accepts the entry?** The response will have an inner `error` for the trigger status, but the entry might be filled. We then have an unprotected position. My code marks `filled=False` in that case, but the entry might already be live on the exchange. **I should detect this and immediately cancel the orphan entry.**

c. **What about reducing existing positions vs opening new ones?** My code always uses `reduce_only=False` for the entry. If the user already has a BTC long and the signal says short, this would open a *new* short position (or flip, depending on size). Maybe I should check current positions first.

d. **State persistence.** `RiskState` (consecutive losses, daily/weekly P&L) is held in memory only. A bot restart loses it. Risk module doc acknowledges this is "persist between bot restarts" but I haven't built the persistence.

e. **Concurrent execution.** If two signals fire in the same second, two `bulk_orders` calls race. Need locking or sequencing at the loop level (not the order manager's job, but the loop's).

### 7. The risk module itself

I noticed `RiskManager.check_trade()` checks `daily_pnl_usd` against `daily_loss_limit_pct`. But `daily_pnl_usd()` is `sum(t.pnl_usd for t in closed_trades_today)`. **Unrealized P&L on open positions doesn't count.** Is that intentional? In a real cascade, we could be -$200 unrealized and still get a green light to take a new trade.

## How to test

1. **Read the code top-to-bottom** (~5 min). It's short.
2. **Run the tests**: `cd C:\Users\AbuBa\Desktop\HyphyLiquid && .\venv\Scripts\python.exe -m pytest tests/test_order_manager.py -v`
3. **Walk through sizing math** with a concrete example.
4. **Spot the gaps** — what could go wrong that I haven't tested? (Don't be polite. Find at least 2 issues.)

## What to look for in particular

- Is the position-sizing formula correct in all corner cases (sl_distance=0, sl_distance > entry, leverage cap binding)?
- Does the `bulk_orders` call shape match HL's current API? (HL SDK just changed `coin=` to `name=` recently — anything similar here?)
- Are the trigger order params correct? `tpsl: "tp"` vs `"sl"` — string enum or object?
- Is the atomic placement actually atomic? What if HL partially fills?

## Specific questions to answer

1. **Verdict:** ship it, fix it, or trash it?
2. **Top 3 issues** ranked by severity, with line numbers.
3. **What tests am I missing** that would catch the worst real-world failure mode?
4. **Is the API contract for `OrderResult` complete** — what does a downstream caller (the live loop) need that I'm not returning?

## Context

This is a $1,000 USDC bankroll project. The order manager has NOT been used to place a real trade yet. The auth spike proved the SDK wires (we can place + cancel tiny orders on testnet), but the order manager's full flow — signal → risk check → atomic entry+TP+SL — has only been unit-tested with fakes.

The user has ~$1,000 of mock USDC on Hyperliquid testnet (`0x966179487b7D09690Aeb8b88640B5e6D9a549C8B` per the latest wallet config). They can run a live test whenever they want.

Don't worry about the rest of the project (liquidation detector, HyperPerps poller, backtest harness) — those are out of scope for this review. Focus on the order manager.
